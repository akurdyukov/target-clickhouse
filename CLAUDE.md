# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Singer target that loads data into ClickHouse, built on the Meltano Singer SDK (`singer-sdk` `>=0.40,<0.45`) and `clickhouse-sqlalchemy` (pinned to a git commit in `pyproject.toml`). Published to PyPI as `shaped-target-clickhouse`; the CLI entry point is `target-clickhouse` (`target_clickhouse.target:TargetClickhouse.cli`). Python `>=3.11,<3.12` (3.11 only); if the system Python is newer, `poetry python install 3.11 && poetry env use 3.11` (Poetry refuses to lock or install with an out-of-range interpreter).

## Commands

```bash
poetry install                        # set up dev env
./docker_run_clickhouse.sh            # start ClickHouse on localhost:18123 (http) / 19000 (native) — required for tests
poetry run pytest                     # run all tests
poetry run pytest tests/test_validation.py::test_validation_string_conversion   # single test
poetry run pytest tests/test_core.py -k Spread                                  # subset of SDK standard tests
poetry run ruff check .               # lint (also: tox -e lint; tox -e format to autofix)
poetry run target-clickhouse --about  # print config schema/capabilities
meltano run tap-smoke-test target-clickhouse   # end-to-end smoke test (CI "integration" job)
```

- Tests in `tests/test_core.py` and `tests/test_clickhouse_features.py` need a live ClickHouse on port 18123 (and 19000 for the native driver). `test_engine_class.py`, `test_connection_url.py`, `test_missing_values.py`, `test_nullable_types.py` and `test_validation.py` are pure unit tests and need no DB.
- Ruff runs with `select = ["ALL"]` plus an ignore list in `pyproject.toml`; pre-commit also runs mypy.
- CI tests Python 3.11 against `clickhouse/clickhouse-server:23.4-alpine`; lint and the Meltano integration job also run on 3.11.
- `setuptools` is capped at `<=70.3.0` by singer-sdk 0.44, so its Dependabot alerts need a singer-sdk upgrade (beyond the current `<0.45` pin).
- Releases: creating a GitHub release triggers `release_workflow.yml`, which uses `poetry-dynamic-versioning` (version comes from the git tag) and publishes to PyPI.

## Architecture

Three SDK subclasses, wired together as `TargetClickhouse.default_sink_class = ClickhouseSink` and `ClickhouseSink.connector_class = ClickhouseConnector`:

- **`target.py` — `TargetClickhouse(SQLTarget)`**: only declares `config_jsonschema`. When adding a config option, add it here and update the README settings table (generated from `target-clickhouse --about --format=markdown`).
- **`connectors.py` — `ClickhouseConnector(SQLConnector)`**: all DDL/connection behavior.
  - Builds the SQLAlchemy URL from discrete settings (`driver` = http/native/asynch, `secure`, `verify`) unless `sqlalchemy_url` is given.
  - `_connect` sets `stream_results` only for the http driver. With the native driver it routes batch INSERTs through `clickhouse_driver.execute_iter`, which fails ("Parameters are expected in dict form"); the driver is detected from the engine URL, so a `clickhouse+native` `sqlalchemy_url` counts.
  - https URLs omit `verify` unless it's false: the http driver passes any other value to `requests` as a CA bundle path.
  - `to_sql_type(jsonschema_type, is_primary_key=...)`: `DECIMAL` → `Float64` (ClickHouse needs precision for Decimal; a generic `FLOAT` would be `Float32`), `_adapt_column_type` widens existing `Float32` columns, `INTEGER` → `Int64`, `DATE` → `Date32` (supports pre-epoch dates). Date/time types, and any type whose JSON schema allows `null`, are wrapped in `Nullable(...)` — except primary-key columns, which ClickHouse requires to be non-nullable. Without `Nullable`, ClickHouse silently casts NULL to a default value (e.g. epoch for dates).
  - `create_empty_table` builds a `clickhouse_sqlalchemy.Table` with an engine from `engine_class.py`, honors `table_name` and `cluster_name` config, ignores schema (`prepare_schema` is a no-op — a ClickHouse "schema" is a database). Temp tables raise `NotImplementedError`.
  - Column add/alter DDL overridden for `ADD COLUMN IF NOT EXISTS` and `ON CLUSTER`.
- **`engine_class.py`**: `SupportedEngines` enum ↔ `clickhouse_sqlalchemy.engines` mapping. Default engine is `MergeTree`. Primary keys become the engine `primary_key`; with no keys it uses `ORDER BY tuple()`; the `order_by_keys` config overrides `order_by`. `Replicated*` engines require `table_path` (supports `$table_name` substitution) and `replica_name`.
- **`sinks.py` — `ClickhouseSink(SQLSink)`**:
  - `bulk_insert_records` serializes dict/list values to JSON strings and fills missing values of non-nullable columns with ClickHouse type defaults (`fill_missing_non_nullable_values`; the server does this over http, the native driver would fail on `None`); optionally runs `OPTIMIZE TABLE` (`optimize_after`).
  - `activate_version` uses ClickHouse mutations (`ALTER TABLE ... DELETE` / `ALTER TABLE ... UPDATE`, asynchronous) for hard/soft deletes of versions strictly older than the activated one. `hard_delete` defaults to false, as in the SDK. The SDK calls this without flushing buffered records first, so rows of the current version may or may not be in the table yet.
  - Datetime parse errors are treated as NULL; `_parse_timestamps_in_record` is overridden to repair invalid date values.
  - `_validate_and_parse` first runs module-level `pre_validate_for_string_type`, which recursively coerces values to strings where the schema expects `string` (dict/list → JSON, else `str()`), descending into nested objects and arrays of objects. This is what `tests/test_validation.py` covers.

`tests/test_core.py` relies on the SDK's `get_target_test_class` standard test suite, run against both a `sqlalchemy_url` config and a spread (discrete-settings) config, plus `custom_target_test_suite` from `tests/target_test_cases.py`. To add an end-to-end case: subclass `TargetClickhouseFileTestTemplate` (`conftest.py`), put the input at `tests/target_test_streams/<name>.singer`, and add the class to `custom_target_test_suite`.
