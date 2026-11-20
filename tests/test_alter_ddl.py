"""Unit tests for column type mapping, ALTER DDL and when ALTERs happen (no database).

Existing tables are simulated: the column's ClickHouse type string (as SHOW CREATE
TABLE prints it) is parsed with the dialect's own reflection parser, and executed
statements are recorded instead of sent to a server.
"""

from __future__ import annotations

import contextlib
import logging

import pytest
import sqlalchemy
from clickhouse_sqlalchemy import types

from target_clickhouse.connectors import ClickhouseConnector

URL = "clickhouse+http://default:@localhost:1/default"

# JSON schemas as MeltanoLabs tap-postgres emits them.
INT = {"type": ["integer"]}
INT_NULL = {"type": ["integer", "null"]}
NUM = {"type": ["number"]}
NUM_NULL = {"type": ["number", "null"]}
STR = {"type": ["string"]}
STR_NULL = {"type": ["string", "null"]}
DATETIME_NULL = {"type": ["string", "null"], "format": "date-time"}
DATE_NULL = {"type": ["string", "null"], "format": "date"}
BOOL_NULL = {"type": ["boolean", "null"]}
JSONB = {
    "type": ["string", "number", "integer", "array", "object", "boolean"],
    "additionalProperties": True,
}
JSONB_OBJECT = {"type": ["object", "null"], "additionalProperties": True}


def _connector(**config) -> ClickhouseConnector:
    return ClickhouseConnector(config={"sqlalchemy_url": URL, **config})


def _compile(connector: ClickhouseConnector, element) -> str:
    return str(element.compile(dialect=connector._engine.dialect))  # noqa: SLF001


# --- get_column_alter_ddl -------------------------------------------------------


def test_alter_ddl_on_cluster():
    """With cluster_name, the ALTER runs ON CLUSTER and is a valid statement."""
    connector = _connector(cluster_name="test_cluster")
    ddl = connector.get_column_alter_ddl(
        table_name="db.events",
        column_name="amount",
        column_type=types.Nullable(types.Float64()),
    )
    assert _compile(connector, ddl) == (
        "ALTER TABLE db.events ON CLUSTER test_cluster "
        "MODIFY COLUMN amount Nullable(Float64)"
    )


def test_alter_ddl_without_cluster():
    """Without cluster_name, a plain ALTER TABLE ... MODIFY COLUMN."""
    connector = _connector()
    ddl = connector.get_column_alter_ddl(
        table_name="db.t",
        column_name="code",
        column_type=sqlalchemy.types.VARCHAR(),
    )
    assert _compile(connector, ddl) == (
        "ALTER TABLE db.t MODIFY COLUMN code VARCHAR"
    )


def test_alter_ddl_accepts_type_string():
    """A pre-compiled type string is used as-is."""
    connector = _connector(cluster_name="test_cluster")
    ddl = connector.get_column_alter_ddl(
        table_name="db.t",
        column_name="amount",
        column_type="Nullable(Float64)",
    )
    assert _compile(connector, ddl).endswith("MODIFY COLUMN amount Nullable(Float64)")


def test_alter_ddl_quotes_cluster_name_like_create_table():
    """Cluster names are quoted the way the dialect quotes them in CREATE TABLE."""
    connector = _connector(cluster_name="my-cluster")
    ddl = connector.get_column_alter_ddl(
        table_name="db.t",
        column_name="c",
        column_type="String",
    )
    assert 'ON CLUSTER "my-cluster" MODIFY COLUMN' in _compile(connector, ddl)


# --- type mapping -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("schema", "is_primary_key", "expected"),
    [
        (INT, True, "Int64"),
        (INT_NULL, True, "Int64"),
        (INT, False, "Int64"),
        (INT_NULL, False, "Nullable(Int64)"),
        (NUM, False, "Float64"),
        (NUM_NULL, False, "Nullable(Float64)"),
        (STR, False, "VARCHAR"),
        (STR_NULL, False, "Nullable(VARCHAR)"),
        (DATETIME_NULL, False, "Nullable(DATETIME)"),
        (DATE_NULL, False, "Nullable(Date32)"),
        (BOOL_NULL, False, "Nullable(BOOLEAN)"),
        (JSONB, False, "VARCHAR"),
        (JSONB_OBJECT, False, "Nullable(VARCHAR)"),
    ],
)
def test_type_mapping(schema, is_primary_key, expected):
    """Column types for tap-postgres schemas (same with singer-sdk 0.44 and 0.54)."""
    connector = _connector()
    sql_type = connector.to_sql_type(schema, is_primary_key=is_primary_key)
    assert _compile(connector, sql_type) == expected


# --- when prepare_column alters an existing column ----------------------------------


@contextlib.contextmanager
def _existing_column(connector: ClickhouseConnector, clickhouse_type: str):
    """Make `connector` see column `c` of type `clickhouse_type`; yield executed SQL."""
    reflected = connector._engine.dialect._get_column_type("c", clickhouse_type)  # noqa: SLF001
    executed: list[str] = []

    class _Conn:
        @contextlib.contextmanager
        def begin(self):
            yield self

        def execute(self, statement, *_args: object, **_kwargs: object):
            executed.append(_compile(connector, statement))

    @contextlib.contextmanager
    def fake_connect():
        yield _Conn()

    connector.column_exists = lambda *_a, **_k: True  # type: ignore[method-assign]
    connector.get_table_columns = (  # type: ignore[method-assign]
        lambda *_a, **_k: {"c": sqlalchemy.Column("c", reflected)}
    )
    connector._connect = fake_connect  # type: ignore[method-assign]  # noqa: SLF001
    yield executed


@pytest.mark.parametrize(
    ("schema", "existing"),
    [
        # Types the current and earlier versions create for tap-postgres columns.
        (INT, "Int64"),
        (INT_NULL, "Int64"),
        (INT_NULL, "Nullable(Int64)"),
        (NUM_NULL, "Float64"),
        (NUM_NULL, "Nullable(Float64)"),
        (STR_NULL, "String"),
        (STR_NULL, "Nullable(String)"),
        (DATETIME_NULL, "DateTime"),
        (DATETIME_NULL, "Nullable(DateTime)"),
        (DATE_NULL, "Date"),
        (DATE_NULL, "Nullable(Date32)"),
        (BOOL_NULL, "Bool"),
        (BOOL_NULL, "Nullable(Bool)"),
        (JSONB, "String"),
        (JSONB_OBJECT, "Nullable(String)"),
        # Numbers created as Float32 before they became Float64.
        (NUM, "Float32"),
        (NUM_NULL, "Float32"),
        (NUM_NULL, "Nullable(Float32)"),
    ],
)
def test_existing_columns_are_not_altered(schema, existing):
    """Columns created by this or earlier versions are never ALTERed."""
    connector = _connector(cluster_name="test_cluster")
    with _existing_column(connector, existing) as executed:
        connector.prepare_column("db.t", "c", connector.to_sql_type(schema))
    assert executed == []


def test_float32_column_logs_ddl_for_manual_widening(caplog):
    """For a Float32 number column, the ALTER to widen it is logged, not run."""
    connector = _connector(cluster_name="test_cluster")
    with caplog.at_level(logging.WARNING), _existing_column(
        connector,
        "Nullable(Float32)",
    ) as executed:
        connector.prepare_column("db.t", "c", connector.to_sql_type(NUM_NULL))

    assert executed == []
    assert (
        "ALTER TABLE db.t ON CLUSTER test_cluster MODIFY COLUMN c Nullable(Float64)"
        in caplog.text
    )


def test_real_type_change_runs_valid_alter_on_cluster():
    """A real source type change (integer -> string) runs a valid ALTER ON CLUSTER."""
    connector = _connector(cluster_name="test_cluster")
    with _existing_column(connector, "Int64") as executed:
        connector.prepare_column("db.t", "c", connector.to_sql_type(STR))
    assert executed == [
        "ALTER TABLE db.t ON CLUSTER test_cluster MODIFY COLUMN c VARCHAR",
    ]
