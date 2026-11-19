"""Unit tests for filling missing values of non-nullable columns (no database)."""

from __future__ import annotations

from target_clickhouse.sinks import fill_missing_non_nullable_values

SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "integer"},
        "name": {"type": "string"},
        "price": {"type": "number"},
        "active": {"type": "boolean"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "nickname": {"type": ["string", "null"]},
        "created_at": {"type": "string", "format": "date-time"},
    },
}


def test_missing_non_nullable_values_get_type_defaults():
    """Missing or None values of non-nullable columns get ClickHouse defaults."""
    record = fill_missing_non_nullable_values({"id": 1, "name": None}, SCHEMA)
    assert record == {
        "id": 1,
        "name": "",
        "price": 0.0,
        "active": False,
        "tags": "",
    }


def test_nullable_and_date_columns_stay_missing():
    """Nullable columns and date/time columns (created Nullable) keep NULL."""
    record = fill_missing_non_nullable_values({"nickname": None}, SCHEMA)
    assert record["nickname"] is None
    assert "created_at" not in record


def test_present_values_are_kept():
    """Values that are present, including falsy ones, are not touched."""
    record = {"id": 0, "name": "x", "price": 0.0, "active": True, "tags": "[]"}
    assert fill_missing_non_nullable_values(dict(record), SCHEMA) == record
