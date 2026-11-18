"""Unit tests for table engine selection (no database needed).

The engine is rendered to CREATE TABLE DDL with the ClickHouse dialect, so the
assertions check the SQL that would actually be sent to ClickHouse.
"""

from __future__ import annotations

import pytest
from clickhouse_sqlalchemy import Table, types
from clickhouse_sqlalchemy.drivers.http.base import ClickHouseDialect_http
from sqlalchemy import Column, MetaData
from sqlalchemy.schema import CreateTable

from target_clickhouse.engine_class import SupportedEngines, create_engine_wrapper

REPLICATED_CONFIG = {
    "table_path": "/clickhouse/tables/{shard}/$table_name",
    "replica_name": "{replica}",
}


def _render_ddl(engine, primary_keys: list[str] | None = None) -> str:
    """Render CREATE TABLE DDL for a 3-column table, with whitespace collapsed."""
    primary_keys = primary_keys or []
    table = Table(
        "my_table",
        MetaData(),
        Column("id", types.Int64, primary_key="id" in primary_keys),
        Column("a", types.String),
        Column("b", types.String),
        engine,
    )
    ddl = str(CreateTable(table).compile(dialect=ClickHouseDialect_http()))
    return " ".join(ddl.split())


def test_default_merge_tree_orders_by_primary_key():
    """With key properties, MergeTree sorts and indexes by them."""
    engine = create_engine_wrapper(
        engine_type=SupportedEngines.MERGE_TREE,
        primary_keys=["id"],
        table_name="my_table",
        config={},
    )
    ddl = _render_ddl(engine, primary_keys=["id"])
    assert "ENGINE = MergeTree() ORDER BY id PRIMARY KEY id" in ddl


def test_no_primary_key_orders_by_tuple():
    """Without key properties, ClickHouse requires ORDER BY tuple()."""
    engine = create_engine_wrapper(
        engine_type=SupportedEngines.MERGE_TREE,
        primary_keys=[],
        table_name="my_table",
        config={},
    )
    ddl = _render_ddl(engine)
    assert "ORDER BY tuple()" in ddl
    assert "PRIMARY KEY" not in ddl


def test_order_by_keys_override_sorting_key():
    """The order_by_keys setting replaces the default sorting key."""
    engine = create_engine_wrapper(
        engine_type=SupportedEngines.MERGE_TREE,
        primary_keys=[],
        table_name="my_table",
        config={},
        order_by_keys=["b", "a"],
    )
    ddl = _render_ddl(engine)
    assert "ORDER BY (b, a)" in ddl
    assert "tuple()" not in ddl


@pytest.mark.parametrize(
    "engine_type",
    [
        SupportedEngines.REPLACING_MERGE_TREE,
        SupportedEngines.SUMMING_MERGE_TREE,
        SupportedEngines.AGGREGATING_MERGE_TREE,
    ],
)
def test_non_replicated_engines(engine_type):
    """Each non-replicated engine renders under its own name."""
    engine = create_engine_wrapper(
        engine_type=engine_type,
        primary_keys=["id"],
        table_name="my_table",
        config={},
    )
    assert f"ENGINE = {engine_type.value}(" in _render_ddl(engine, ["id"])


@pytest.mark.parametrize(
    "engine_type",
    [
        SupportedEngines.REPLICATED_MERGE_TREE,
        SupportedEngines.REPLICATED_REPLACING_MERGE_TREE,
        SupportedEngines.REPLICATED_SUMMING_MERGE_TREE,
        SupportedEngines.REPLICATED_AGGREGATING_MERGE_TREE,
    ],
)
def test_replicated_engines_substitute_table_name(engine_type):
    """Replicated engines get table_path (with $table_name filled) and replica."""
    engine = create_engine_wrapper(
        engine_type=engine_type,
        primary_keys=["id"],
        table_name="my_table",
        config=REPLICATED_CONFIG,
    )
    ddl = _render_ddl(engine, primary_keys=["id"])
    assert (
        f"ENGINE = {engine_type.value}"
        "('/clickhouse/tables/{shard}/my_table', '{replica}')"
    ) in ddl


@pytest.mark.parametrize("missing", ["table_path", "replica_name"])
def test_replicated_engine_requires_settings(missing):
    """Replicated engines fail fast when a required setting is missing."""
    config = {k: v for k, v in REPLICATED_CONFIG.items() if k != missing}
    with pytest.raises(ValueError, match=missing):
        create_engine_wrapper(
            engine_type=SupportedEngines.REPLICATED_MERGE_TREE,
            primary_keys=["id"],
            table_name="my_table",
            config=config,
        )


def test_unsupported_engine_rejected():
    """Unknown engine names raise instead of silently using a default."""
    with pytest.raises(ValueError, match="not supported"):
        create_engine_wrapper(
            engine_type="Log",
            primary_keys=[],
            table_name="my_table",
            config={},
        )


def test_config_is_optional():
    """create_engine_wrapper's config argument defaults to None and must work."""
    engine = create_engine_wrapper(
        engine_type=SupportedEngines.MERGE_TREE,
        primary_keys=["id"],
        table_name="my_table",
    )
    assert "ENGINE = MergeTree()" in _render_ddl(engine, ["id"])
