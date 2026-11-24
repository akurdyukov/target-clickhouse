"""Tests against a ClickHouse cluster, with replicated-table loader settings.

Needs a server started with tests/clickhouse/cluster.xml (a one-node cluster named
`test_cluster` with embedded Keeper), reachable on localhost:18123. Skipped unless
CLICKHOUSE_CLUSTER is set. In CI this is the `cluster` job; locally:

    docker run --rm -p 18123:8123 -p 19000:9000 -e CLICKHOUSE_SKIP_USER_SETUP=1 \
      -v "$PWD/tests/clickhouse/cluster.xml:/etc/clickhouse-server/config.d/c.xml" \
      clickhouse/clickhouse-server:26.8-alpine
    CLICKHOUSE_CLUSTER=test_cluster poetry run pytest tests/test_cluster.py
"""

from __future__ import annotations

import os
import typing as t
import uuid

import pytest
import sqlalchemy

from tests.test_clickhouse_features import (
    CLICKHOUSE_URL,
    _query,
    _record,
    _run_target,
    _schema,
    _wait_for_mutations,
)

CLUSTER = os.environ.get("CLICKHOUSE_CLUSTER")
pytestmark = pytest.mark.skipif(
    not CLUSTER,
    reason="needs a ClickHouse cluster; set CLICKHOUSE_CLUSTER (see module docstring)",
)

# Loader settings for replicated tables on a cluster (connection aside).
CLUSTER_CONFIG = {
    "cluster_name": CLUSTER,
    "engine_type": "ReplicatedReplacingMergeTree",
    "table_path": "/clickhouse/tables/{shard}/$table_name",
    "replica_name": "{replica}",
    "optimize_after": True,
}

# A stream as MeltanoLabs tap-postgres describes it: key `id`, numeric columns,
# a nullable timestamp, a boolean and a jsonb column (default mapping).
SAMPLE_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": ["integer"]},
        "price": {"type": ["number", "null"]},
        "ratio": {"type": ["number", "null"]},
        "label": {"type": ["string", "null"]},
        "created_at": {"type": ["string", "null"], "format": "date-time"},
        "is_active": {"type": ["boolean", "null"]},
        "payload": {
            "type": ["string", "number", "integer", "array", "object", "boolean"],
            "additionalProperties": True,
        },
    },
}

SAMPLE_RECORD = {
    "id": 1,
    "price": 1234567.891,
    "ratio": 0.25,
    "label": "a",
    "created_at": "2024-01-01T00:00:00+00:00",
    "is_active": True,
    "payload": {"k": "v"},
}


@pytest.fixture(scope="module")
def engine() -> t.Iterator[sqlalchemy.engine.Engine]:
    eng = sqlalchemy.create_engine(CLICKHOUSE_URL)
    yield eng
    eng.dispose()


@pytest.fixture
def table(engine) -> t.Iterator[str]:
    """A fresh table name; unique per run because Keeper paths outlive DROP."""
    name = f"cluster_{uuid.uuid4().hex[:12]}"
    yield name
    _query(engine, f"DROP TABLE IF EXISTS {name} ON CLUSTER {CLUSTER} SYNC")


def _show_create(engine, table: str) -> str:
    return _query(engine, f"SHOW CREATE TABLE {table}")[0][0]


def _load(table: str, schema: dict, records: list[dict]) -> None:
    _run_target(
        CLUSTER_CONFIG,
        [_schema(table, schema), *(_record(table, r) for r in records)],
    )


def test_table_from_before_float64_change_is_loaded_without_alter(engine, table):
    """Tables created before numbers became Float64 load as-is, with no ALTER.

    Such tables have Float32 number columns; the loader used to try
    `ALTER TABLE ... ON CLUSTER ... MODIFY COLUMN` on them, which failed.
    """
    # The DDL versions before the Float64 change produced for SAMPLE_SCHEMA.
    _query(
        engine,
        f"CREATE TABLE {table} ON CLUSTER {CLUSTER} ("
        " id Int64, price Float32, ratio Float32, label String,"
        " created_at DateTime, is_active Bool, payload String"
        ") ENGINE = ReplicatedReplacingMergeTree("
        f"'/clickhouse/tables/{{shard}}/{table}', '{{replica}}')"
        " ORDER BY id PRIMARY KEY id",
    )
    before = _show_create(engine, table)

    _load(table, SAMPLE_SCHEMA, [SAMPLE_RECORD])
    _wait_for_mutations(engine, table)

    assert _show_create(engine, table) == before
    assert _query(engine, f"SELECT id, label FROM {table}") == [(1, "a")]


def test_table_from_current_version_is_reloaded_without_alter(engine, table):
    """A table this version created is not altered on the next load."""
    _load(table, SAMPLE_SCHEMA, [SAMPLE_RECORD])
    _wait_for_mutations(engine, table)
    before = _show_create(engine, table)

    _load(table, SAMPLE_SCHEMA, [{**SAMPLE_RECORD, "id": 2}])
    _wait_for_mutations(engine, table)

    assert _show_create(engine, table) == before
    assert _query(engine, f"SELECT id FROM {table} ORDER BY id") == [(1,), (2,)]


def test_real_type_change_alters_column_on_cluster(engine, table):
    """A real source type change runs a valid ALTER ... ON CLUSTER ... MODIFY COLUMN."""
    int_schema = {
        "type": "object",
        "properties": {"id": {"type": ["integer"]}, "code": {"type": ["integer"]}},
    }
    _load(table, int_schema, [{"id": 1, "code": 42}])
    assert "`code` Int64" in _show_create(engine, table)

    # The source column became text (non-nullable, so the SDK widens the type).
    str_schema = {
        "type": "object",
        "properties": {"id": {"type": ["integer"]}, "code": {"type": ["string"]}},
    }
    _load(table, str_schema, [{"id": 2, "code": "A-7"}])
    _wait_for_mutations(engine, table)

    assert "`code` String" in _show_create(engine, table)
    assert _query(engine, f"SELECT id, code FROM {table} ORDER BY id") == [
        (1, "42"),
        (2, "A-7"),
    ]
    # It went through distributed DDL, i.e. the statement had ON CLUSTER.
    queued = [
        row[0]
        for row in _query(
            engine,
            "SELECT DISTINCT query FROM system.distributed_ddl_queue "
            f"WHERE query LIKE '%{table}%'",
        )
    ]
    alters = [q for q in queued if "MODIFY COLUMN" in q.upper()]
    assert alters, f"no ALTER for {table} in the distributed DDL queue: {queued}"
    assert f"ON CLUSTER {CLUSTER}" in alters[0], alters[0]
