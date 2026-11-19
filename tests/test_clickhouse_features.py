"""End-to-end tests of target settings against a live ClickHouse.

Each test runs the target in-process on a list of Singer messages, then reads the
result back from ClickHouse. Requires the server from ./docker_run_clickhouse.sh
(or the CI service) on localhost:18123.
"""

from __future__ import annotations

import io
import json
import re
import time
import typing as t

import pytest
import sqlalchemy
from singer_sdk.testing import target_sync_test

from target_clickhouse.target import TargetClickhouse

CLICKHOUSE_URL = "clickhouse+http://default:@localhost:18123/default"
MUTATION_TIMEOUT_SECONDS = 30

ID_VALUE_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "integer"},
        "value": {"type": ["string", "null"]},
    },
}


@pytest.fixture(scope="module")
def engine() -> t.Iterator[sqlalchemy.engine.Engine]:
    eng = sqlalchemy.create_engine(CLICKHOUSE_URL)
    yield eng
    eng.dispose()


def _query(engine, sql: str) -> list[tuple]:
    with engine.connect() as conn:
        return [tuple(row) for row in conn.execute(sqlalchemy.text(sql)).fetchall()]


@pytest.fixture()
def table(engine, request) -> t.Iterator[str]:
    """A unique table/stream name, dropped before and after the test."""
    name = "it_" + re.sub(r"\W", "_", request.node.name).lower()
    _query(engine, f"DROP TABLE IF EXISTS {name}")
    yield name
    _query(engine, f"DROP TABLE IF EXISTS {name}")


def _run_target(config: dict, messages: list[dict]) -> None:
    target = TargetClickhouse(config={"sqlalchemy_url": CLICKHOUSE_URL, **config})
    lines = "\n".join(json.dumps(m) for m in messages) + "\n"
    target_sync_test(target, io.StringIO(lines))


def _schema(stream: str, schema: dict, keys: list[str] | None = None) -> dict:
    return {
        "type": "SCHEMA",
        "stream": stream,
        "schema": schema,
        "key_properties": keys if keys is not None else ["id"],
    }


def _record(stream: str, record: dict, version: int | None = None) -> dict:
    message = {"type": "RECORD", "stream": stream, "record": record}
    if version is not None:
        message["version"] = version
    return message


def _activate(stream: str, version: int) -> dict:
    return {"type": "ACTIVATE_VERSION", "stream": stream, "version": version}


def _wait_for_mutations(engine, table: str) -> None:
    """ALTER TABLE DELETE/UPDATE run asynchronously; wait until they finish."""
    deadline = time.monotonic() + MUTATION_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        pending = _query(
            engine,
            "SELECT count() FROM system.mutations "
            f"WHERE database = currentDatabase() AND table = '{table}' "
            "AND is_done = 0",
        )
        if pending[0][0] == 0:
            return
        time.sleep(0.2)
    pytest.fail(f"Mutations on {table} did not finish in time")


def _column_types(engine, table: str) -> dict[str, str]:
    return dict(
        _query(
            engine,
            "SELECT name, type FROM system.columns "
            f"WHERE database = currentDatabase() AND table = '{table}'",
        ),
    )


def test_number_keeps_double_precision(engine, table):
    """JSON `number` values must not be truncated to 32-bit floats."""
    value = 1234567.891
    schema = {
        "type": "object",
        "properties": {"id": {"type": "integer"}, "amount": {"type": "number"}},
    }
    _run_target(
        {},
        [_schema(table, schema), _record(table, {"id": 1, "amount": value})],
    )

    assert _query(engine, f"SELECT amount FROM {table}") == [(value,)]
    assert "Float64" in _column_types(engine, table)["amount"]


def test_existing_float32_column_is_widened(engine, table):
    """A Float32 column made by older versions is widened and loads full precision."""
    _query(
        engine,
        f"CREATE TABLE {table} (id Int64, amount Float32) "
        "ENGINE = MergeTree ORDER BY id",
    )
    _query(engine, f"INSERT INTO {table} VALUES (1, 1.5)")
    value = 1234567.891
    schema = {
        "type": "object",
        "properties": {"id": {"type": "integer"}, "amount": {"type": "number"}},
    }
    _run_target(
        {},
        [_schema(table, schema), _record(table, {"id": 2, "amount": value})],
    )
    _wait_for_mutations(engine, table)

    assert "Float64" in _column_types(engine, table)["amount"]
    assert _query(engine, f"SELECT id, amount FROM {table} ORDER BY id") == [
        (1, 1.5),
        (2, value),
    ]


def test_native_driver_via_sqlalchemy_url(engine, table):
    """A clickhouse+native sqlalchemy_url can insert, not just the driver setting."""
    _run_target(
        {"sqlalchemy_url": "clickhouse+native://default:@localhost:19000/default"},
        [
            _schema(table, ID_VALUE_SCHEMA),
            _record(table, {"id": 1, "value": "a"}),
            _record(table, {"id": 2, "value": "b"}),
        ],
    )

    assert _query(engine, f"SELECT id, value FROM {table} ORDER BY id") == [
        (1, "a"),
        (2, "b"),
    ]


def test_table_name_setting_overrides_stream_name(engine, table):
    """With table_name set, records go to that table, not to the stream's name."""
    stream = f"{table}_stream"
    _query(engine, f"DROP TABLE IF EXISTS {stream}")
    _run_target(
        {"table_name": table},
        [
            _schema(stream, ID_VALUE_SCHEMA),
            _record(stream, {"id": 1, "value": "a"}),
            _record(stream, {"id": 2, "value": "b"}),
        ],
    )

    assert _query(engine, f"SELECT id, value FROM {table} ORDER BY id") == [
        (1, "a"),
        (2, "b"),
    ]
    assert _query(engine, f"EXISTS TABLE {stream}") == [(0,)]


def test_order_by_keys_sets_sorting_key(engine, table):
    """order_by_keys becomes the table's sorting key."""
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
    }
    _run_target(
        {"order_by_keys": ["b", "a"]},
        [_schema(table, schema, keys=[]), _record(table, {"a": "x", "b": "y"})],
    )

    assert _query(
        engine,
        "SELECT sorting_key FROM system.tables "
        f"WHERE database = currentDatabase() AND name = '{table}'",
    ) == [("b, a",)]


def test_replacing_merge_tree_with_optimize_after_dedupes(engine, table):
    """ReplacingMergeTree + optimize_after leaves only the latest row per key."""
    config = {"engine_type": "ReplacingMergeTree", "optimize_after": True}
    for value in ("old", "new"):
        _run_target(
            config,
            [
                _schema(table, ID_VALUE_SCHEMA),
                _record(table, {"id": 1, "value": value}),
            ],
        )

    assert _query(
        engine,
        "SELECT engine FROM system.tables "
        f"WHERE database = currentDatabase() AND name = '{table}'",
    ) == [("ReplacingMergeTree",)]
    assert _query(engine, f"SELECT id, value FROM {table}") == [(1, "new")]


# ACTIVATE_VERSION handling. `batch_size_rows: 1` flushes each record to ClickHouse
# right away, as happens for any table larger than one batch, so the records are
# already in the table when the closing ACTIVATE_VERSION message is processed.
VERSIONED_CONFIG = {"add_record_metadata": True, "batch_size_rows": 1}


def _full_table_sync(table: str, version: int, ids: list[int]) -> list[dict]:
    """Messages a tap sends for a full-table sync at the given table version."""
    return [
        _schema(table, ID_VALUE_SCHEMA),
        _activate(table, version),
        *(_record(table, {"id": i, "value": str(i)}, version) for i in ids),
        _activate(table, version),
    ]


def test_activate_version_keeps_current_sync_by_default(engine, table):
    """With default settings, a full-table sync keeps the rows it just loaded."""
    _run_target(VERSIONED_CONFIG, _full_table_sync(table, 1, [1, 2, 3]))
    _wait_for_mutations(engine, table)

    assert _query(engine, f"SELECT id FROM {table} ORDER BY id") == [(1,), (2,), (3,)]


def test_activate_version_hard_delete_removes_only_old_versions(engine, table):
    """hard_delete removes rows from previous versions and keeps the current one."""
    config = {**VERSIONED_CONFIG, "hard_delete": True}
    _run_target(config, _full_table_sync(table, 1, [1, 2]))
    _wait_for_mutations(engine, table)
    _run_target(config, _full_table_sync(table, 2, [3]))
    _wait_for_mutations(engine, table)

    assert _query(engine, f"SELECT id FROM {table} ORDER BY id") == [(3,)]


def test_activate_version_soft_delete_marks_old_versions(engine, table):
    """Without hard_delete, only old-version rows get _sdc_deleted_at set."""
    config = {**VERSIONED_CONFIG, "hard_delete": False}
    _run_target(config, _full_table_sync(table, 1, [1, 2]))
    _wait_for_mutations(engine, table)
    _run_target(config, _full_table_sync(table, 2, [3]))
    _wait_for_mutations(engine, table)

    assert _query(
        engine,
        f"SELECT id, _sdc_deleted_at IS NOT NULL FROM {table} ORDER BY id",
    ) == [(1, True), (2, True), (3, False)]
