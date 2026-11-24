from __future__ import annotations

import contextlib
import typing
from typing import TYPE_CHECKING

import sqlalchemy.types
import urllib3
from clickhouse_sqlalchemy import (
    Table,
)
from clickhouse_sqlalchemy import (
    types as clickhouse_sqlalchemy_types,
)
from singer_sdk.sql import SQLConnector
from singer_sdk.sql.connector import JSONSchemaToSQL
from sqlalchemy import Column, MetaData, create_engine

from target_clickhouse.engine_class import SupportedEngines, create_engine_wrapper

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

# Date and time column types; made Nullable (unless a primary key) because
# ClickHouse would otherwise store NULL as the epoch.
_DATELIKE_TYPES = (
    sqlalchemy.types.DATETIME,
    sqlalchemy.types.TIME,
    clickhouse_sqlalchemy_types.Date32,
)


def _allows_type(schema: dict, json_type: str) -> bool:
    """Whether `schema` (or one of its `anyOf` alternatives) allows `json_type`."""
    schema_type = schema.get("type")
    if isinstance(schema_type, (list, tuple)):
        if json_type in schema_type:
            return True
    elif schema_type == json_type:
        return True
    return any(_allows_type(sub, json_type) for sub in schema.get("anyOf", ()))


class ClickhouseJSONSchemaToSQL(JSONSchemaToSQL):
    """Map JSON Schema types to ClickHouse column types.

    The mapping is the one this target has always used (it must not change, or
    existing tables would get ALTERed), which differs from the SDK defaults:

    - `number` is `Float64`: ClickHouse needs a precision for `Decimal`, and a
      generic `FLOAT` is `Float32` (about 7 significant digits).
    - `integer` is `Int64`; `date` is `Date32`, which supports pre-epoch dates.
    - Other string formats (`uuid`, `email`, `x-singer.decimal`, ...) stay strings.
    - With several allowed types, the first of `JSON_TYPE_PRECEDENCE` wins (any
      type that allows `object`, `array` or `string` is a string column), also
      across `anyOf` alternatives; the SDK defaults would use the first matching
      alternative instead.

    `Nullable` is added by `ClickhouseConnector.to_sql_type`.
    """

    #: JSON types in the order they are checked; the first one allowed decides.
    JSON_TYPE_PRECEDENCE = ("object", "array", "string", "integer", "number", "boolean")

    #: Column types for the date-like string formats.
    DATELIKE_FORMATS: typing.ClassVar[dict[str, type[sqlalchemy.types.TypeEngine]]] = {
        "date-time": sqlalchemy.types.DATETIME,
        "time": sqlalchemy.types.TIME,
        "date": clickhouse_sqlalchemy_types.Date32,
    }

    def to_sql_type(self, schema: dict) -> sqlalchemy.types.TypeEngine:
        """Return the ClickHouse type (before `Nullable`) for a JSON Schema.

        Args:
            schema: The JSON Schema of one property.

        Returns:
            The column type.

        """
        handlers: dict[str, typing.Callable[[dict], sqlalchemy.types.TypeEngine]] = {
            "object": lambda _: sqlalchemy.types.VARCHAR(),
            "array": lambda _: sqlalchemy.types.VARCHAR(),
            "string": self._string_type,
            "integer": lambda _: clickhouse_sqlalchemy_types.Int64(),
            "number": lambda _: clickhouse_sqlalchemy_types.Float64(),
            "boolean": lambda _: sqlalchemy.types.BOOLEAN(),
        }
        for json_type in self.JSON_TYPE_PRECEDENCE:
            if _allows_type(schema, json_type):
                return handlers[json_type](schema)
        return self.fallback_type()

    def _string_type(self, schema: dict) -> sqlalchemy.types.TypeEngine:
        # A date-like format on the schema or on one of its string alternatives.
        for candidate in (schema, *schema.get("anyOf", ())):
            if "string" in candidate.get("type", ()):
                type_class = self.DATELIKE_FORMATS.get(candidate.get("format", ""))
                if type_class is not None:
                    return type_class()
        return self.handle_raw_string(schema)


class ClickhouseConnector(SQLConnector):
    """Clickhouse Meltano Connector.

    Inherits from `SQLConnector` class, overriding methods where needed
    for Clickhouse compatibility.
    """

    allow_column_add: bool = True  # Whether ADD COLUMN is supported.
    allow_column_rename: bool = True  # Whether RENAME COLUMN is supported.
    allow_column_alter: bool = True  # Whether altering column types is supported.
    allow_merge_upsert: bool = False  # Whether MERGE UPSERT is supported.
    allow_temp_tables: bool = True  # Whether temp tables are supported.

    jsonschema_to_sql_converter = ClickhouseJSONSchemaToSQL

    def get_sqlalchemy_url(self, config: dict) -> str:
        """Generates a SQLAlchemy URL for clickhouse.

        Args:
            config: The configuration for the connector.

        """
        if config.get("sqlalchemy_url"):
            return super().get_sqlalchemy_url(config)

        if config["driver"] == "http":
            if config["secure"]:
                secure_options = "protocol=https"

                if not config["verify"]:
                    # The http driver only converts verify=False to a boolean; any
                    # other value reaches `requests` as a CA bundle path. So omit
                    # it when verifying (requests verifies by default).
                    secure_options += "&verify=False"

                    # disable urllib3 warning
                    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            else:
                secure_options = "protocol=http"
        else:
            secure_options = f"secure={config['secure']}&verify={config['verify']}"
        return (
            f"clickhouse+{config['driver']}://{config['username']}:{config['password']}@"
            f"{config['host']}:{config['port']}/"
            f"{config['database']}?{secure_options}"
        )

    def create_engine(self) -> Engine:
        """Create a SQLAlchemy engine for clickhouse."""
        return create_engine(self.get_sqlalchemy_url(self.config))

    @contextlib.contextmanager
    def _connect(self) -> typing.Iterator[sqlalchemy.engine.Connection]:
        # The SDK's default _connect sets stream_results=True. With the native
        # driver that makes the cursor use clickhouse_driver's execute_iter, which
        # cannot run INSERTs with a batch of rows ("Parameters are expected in
        # dict form"). So don't stream results with the native driver, however it
        # was selected (`driver` setting or a clickhouse+native sqlalchemy_url).
        if self._engine.url.get_driver_name() == "native":
            kwargs = {}
        else:
            kwargs = {"stream_results": True}
        with self._engine.connect().execution_options(**kwargs) as conn:
            yield conn

    def to_sql_type(
        self,
        jsonschema_type: dict,
        **kwargs,
    ) -> sqlalchemy.types.TypeEngine:
        """Return the ClickHouse column type for a JSON Schema property.

        The base type comes from `ClickhouseJSONSchemaToSQL`; this adds `Nullable`
        where ClickHouse needs it.

        Args:
            jsonschema_type: The JSON Schema representation of the source type.
            **kwargs: `is_primary_key=True` keeps the type non-nullable.

        Returns:
            The SQLAlchemy type representation of the data type.

        """
        sql_type = self.jsonschema_to_sql.to_sql_type(jsonschema_type)
        is_primary_key = kwargs.get("is_primary_key", False)

        # All date and time types should be flagged as Nullable to allow for NULL value.
        if isinstance(sql_type, _DATELIKE_TYPES) and not is_primary_key:
            sql_type = clickhouse_sqlalchemy_types.Nullable(sql_type)

        # Wrap any type in Nullable if the JSON schema allows null values
        # and it's not already Nullable and not a primary key.
        schema_type = jsonschema_type.get("type", [])
        if (
            isinstance(schema_type, list)
            and "null" in schema_type
            and not is_primary_key
            and not isinstance(sql_type, clickhouse_sqlalchemy_types.Nullable)
        ):
            sql_type = clickhouse_sqlalchemy_types.Nullable(sql_type)

        return sql_type

    def create_empty_table(
        self,
        full_table_name: str,
        schema: dict,
        primary_keys: list[str] | None = None,
        partition_keys: list[str] | None = None,
        as_temp_table: bool = False,
    ) -> None:
        """Create an empty target table, using Clickhouse Engine.

        Args:
            full_table_name: the target table name.
            schema: the JSON schema for the new table.
            primary_keys: list of key properties.
            partition_keys: list of partition keys.
            as_temp_table: True to create a temp table.

        Raises:
            NotImplementedError: if temp tables are unsupported and as_temp_table=True.
            RuntimeError: if a variant schema is passed with no properties defined.

        """
        if as_temp_table:
            msg = "Temporary tables are not supported."
            raise NotImplementedError(msg)

        _ = partition_keys  # Not supported in generic implementation.

        _, _, table_name = self.parse_full_table_name(full_table_name)

        # If config table name is set, then use it instead of the table name.
        if self.config.get("table_name"):
            table_name = self.config.get("table_name")

        # Do not set schema, as it is not supported by Clickhouse.
        meta = MetaData(schema=None)

        columns: list[Column] = []
        primary_keys = primary_keys or []

        # If config engine type is set, then use it instead of the default engine type.
        if self.config.get("engine_type"):
            engine_type = self.config.get("engine_type")
        else:
            engine_type = SupportedEngines.MERGE_TREE

        try:
            properties: dict = schema["properties"]
        except KeyError as e:
            msg = f"Schema for '{full_table_name}' does not define properties: {schema}"
            raise RuntimeError(msg) from e
        for property_name, property_jsonschema in properties.items():
            is_primary_key = property_name in primary_keys
            sql_type = self.to_sql_type(
                property_jsonschema,
                is_primary_key=is_primary_key,
            )
            columns.append(
                Column(
                    property_name,
                    sql_type,
                    primary_key=is_primary_key,
                ),
            )

        table_engine = create_engine_wrapper(
            engine_type=engine_type,
            primary_keys=primary_keys,
            table_name=table_name,
            config=self.config,
            order_by_keys=self.config.get("order_by_keys"),
        )

        table_args = {}
        if self.config.get("cluster_name"):
            table_args["clickhouse_cluster"] = self.config.get("cluster_name")

        _ = Table(table_name, meta, *columns, table_engine, **table_args)
        meta.create_all(self._engine)

    def prepare_schema(self, _: str) -> None:
        """Create the target database schema.

        In Clickhouse, a schema is a database, so this method is a no-op.

        Args:
            schema_name: The target schema name.

        """
        return

    def _adapt_column_type(
        self,
        full_table_name: str,
        column_name: str,
        sql_type: sqlalchemy.types.TypeEngine,
    ) -> None:
        """Adapt a column's type to a new JSON schema type, if really needed.

        Versions before PR #51 created JSON `number` columns as Float32; the
        current mapping is Float64. Such columns are deliberately *not* altered:
        changing a column type on (possibly replicated, ON CLUSTER) production
        tables must not happen as a side effect of a load. The ALTER that would
        widen the column is logged so it can be run deliberately. Values keep
        loading into the Float32 column, as with those older versions.

        Everything else goes to the SDK, which only alters when the merged type
        really differs (e.g. a source column changing from integer to string).
        """
        current_type = self._get_column_type(full_table_name, column_name)
        if _float_type(current_type) is clickhouse_sqlalchemy_types.Float32 and (
            _float_type(sql_type) is clickhouse_sqlalchemy_types.Float64
        ):
            # Keep the column's current nullability; only the width changes.
            widened: sqlalchemy.types.TypeEngine
            widened = clickhouse_sqlalchemy_types.Float64()
            if isinstance(current_type, clickhouse_sqlalchemy_types.Nullable):
                widened = clickhouse_sqlalchemy_types.Nullable(widened)
            alter_ddl = self.get_column_alter_ddl(
                table_name=full_table_name,
                column_name=column_name,
                column_type=widened,
            )
            self.logger.warning(
                "Column %s.%s is Float32 (created by an older version); JSON numbers "
                "now map to Float64. Not altering it automatically. To keep full "
                "precision, run: %s",
                full_table_name,
                column_name,
                alter_ddl.compile(dialect=self._engine.dialect),
            )
            return

        super()._adapt_column_type(full_table_name, column_name, sql_type)

    def prepare_column(
        self,
        full_table_name: str,
        column_name: str,
        sql_type: sqlalchemy.types.TypeEngine,
    ) -> None:
        """Adapt target table to provided schema if possible.

        Args:
            full_table_name: the target table name.
            column_name: the target column name.
            sql_type: the SQLAlchemy type.

        """
        if not self.column_exists(full_table_name, column_name):
            self._create_empty_column(
                full_table_name=full_table_name,
                column_name=column_name,
                sql_type=sql_type,
            )
            return

        with contextlib.suppress(NotImplementedError):
            self._adapt_column_type(
                full_table_name,
                column_name=column_name,
                sql_type=sql_type,
            )

    @staticmethod
    def get_column_add_ddl(
        table_name: str,
        column_name: str,
        column_type: sqlalchemy.types.TypeEngine,
    ) -> sqlalchemy.DDL:
        """Get the create column DDL statement.

        Override this if your database uses a different syntax for creating columns.

        Args:
            table_name: Fully qualified table name of column to alter.
            column_name: Column name to create.
            column_type: New column sqlalchemy type.

        Returns:
            A sqlalchemy DDL instance.

        """
        create_column_clause = sqlalchemy.schema.CreateColumn(
            sqlalchemy.Column(
                column_name,
                column_type,
            ),
        )
        return sqlalchemy.DDL(
            (
                "ALTER TABLE %(table_name)s ADD COLUMN IF NOT EXISTS "
                "%(create_column_clause)s"
            ),
            {
                "table_name": table_name,
                "create_column_clause": create_column_clause,
            },
        )

    def get_column_alter_ddl(
        self,
        table_name: str,
        column_name: str,
        column_type: sqlalchemy.types.TypeEngine | str,
    ) -> sqlalchemy.DDL:
        """Get the alter column DDL statement.

        Overrides the static method in the base class to support ON CLUSTER.

        Args:
            table_name: Fully qualified table name of column to alter.
            column_name: Column name to alter.
            column_type: New column type, as a SQLAlchemy type or a type string.

        Returns:
            A sqlalchemy DDL instance.

        """
        dialect = self._engine.dialect
        if isinstance(column_type, sqlalchemy.types.TypeEngine):
            # Render ClickHouse syntax (e.g. Nullable(Float64)), not a generic one.
            column_type = column_type.compile(dialect=dialect)
        context = {
            "table_name": table_name,
            "column_name": column_name,
            "column_type": column_type,
        }
        cluster_name = self.config.get("cluster_name")
        if cluster_name:
            return sqlalchemy.DDL(
                "ALTER TABLE %(table_name)s ON CLUSTER %(cluster_name)s "
                "MODIFY COLUMN %(column_name)s %(column_type)s",
                {
                    **context,
                    # Quoted like the dialect does for CREATE TABLE ... ON CLUSTER.
                    "cluster_name": dialect.identifier_preparer.quote(cluster_name),
                },
            )
        return sqlalchemy.DDL(
            "ALTER TABLE %(table_name)s MODIFY COLUMN %(column_name)s %(column_type)s",
            context,
        )


def _float_type(sql_type: sqlalchemy.types.TypeEngine) -> type | None:
    """Return Float32/Float64 for a (possibly Nullable) ClickHouse float type."""
    if isinstance(sql_type, clickhouse_sqlalchemy_types.Nullable):
        sql_type = sql_type.nested_type
    for float_class in (
        clickhouse_sqlalchemy_types.Float32,
        clickhouse_sqlalchemy_types.Float64,
    ):
        if type(sql_type) is float_class:
            return float_class
    return None
