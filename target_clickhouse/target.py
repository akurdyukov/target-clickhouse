from __future__ import annotations

from singer_sdk import typing as th
from singer_sdk.target_base import SQLTarget

from target_clickhouse.sinks import (
    ClickhouseSink,
)


class TargetClickhouse(SQLTarget):
    """SQL-based target for Clickhouse."""

    name = "target-clickhouse"

    config_jsonschema = th.PropertiesList(
        th.Property(
            "driver",
            th.StringType,
            required=False,
            description="Driver type",
            default="http",
            allowed_values=["http", "native", "asynch"]
        ),
        th.Property(
            "username",
            th.StringType,
            required=False,
            description="Database user",
            default="default",
        ),
        th.Property(
            "password",
            th.StringType,
            required=True,
            description="Username password",
            secret=True
        ),
        th.Property(
            "host",
            th.StringType,
            required=False,
            description="Database host",
            default="localhost"
        ),
        th.Property(
            "port",
            th.IntegerType,
            required=False,
            description="Database connection port",
            default=8123,
        ),
        th.Property(
            "database",
            th.StringType,
            required=False,
            description="Database name",
            default="default",
        ),
        th.Property(
            "secure",
            th.BooleanType,
            description="Should the connection be secure",
            default=False
        ),
        th.Property(
            "verify",
            th.BooleanType,
            description="Should secure connection need to verify SSL/TLS",
            default=True
        ),
        th.Property(
            "table_name",
            th.StringType,
            description="The name of the table to write to. Defaults to stream name.",
        ),
    ).to_dict()

    default_sink_class = ClickhouseSink


if __name__ == "__main__":
    TargetClickhouse.cli()
