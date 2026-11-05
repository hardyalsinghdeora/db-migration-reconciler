from pathlib import Path

import sqlalchemy as sa

from reconciler.dialects import TypeFamily, type_family
from reconciler.schema import compare_schemas
from tests.conftest import DatabasePair, build_items_db, item_metadata, item_rows


def test_identical_schemas_have_no_drift(pair: DatabasePair) -> None:
    report = compare_schemas(*pair.open())

    assert report.ok
    assert report.to_dict() == {"ok": True, "tables": ["items"], "drifts": []}


def test_missing_column_is_reported(tmp_path: Path) -> None:
    rows = [{k: v for k, v in row.items() if k != "note"} for row in item_rows(3)]
    pair = DatabasePair(
        build_items_db(tmp_path / "s.db", item_rows(3)),
        build_items_db(tmp_path / "t.db", rows, item_metadata(drop="note")),
    )
    try:
        report = compare_schemas(*pair.open())
    finally:
        pair.close()

    assert not report.ok
    assert [(d.table, d.column, d.kind, d.target) for d in report.drifts] == [
        ("items", "note", "missing_column", None)
    ]


def test_type_family_mismatch_is_reported_but_same_family_is_not(tmp_path: Path) -> None:
    # VARCHAR -> TEXT stays within the text family; BOOLEAN -> INTEGER crosses families.
    metadata = item_metadata(name=sa.Text(), active=sa.Integer())
    pair = DatabasePair(
        build_items_db(tmp_path / "s.db", item_rows(3)),
        build_items_db(tmp_path / "t.db", item_rows(3), metadata),
    )
    try:
        report = compare_schemas(*pair.open())
    finally:
        pair.close()

    assert [(d.column, d.kind, d.source, d.target) for d in report.drifts] == [
        ("active", "type", "BOOLEAN [bool]", "INTEGER [integer]")
    ]


def test_nullability_extra_column_and_extra_table(tmp_path: Path) -> None:
    target_metadata = item_metadata(nullable=("note", "qty"))
    target_metadata.tables["items"].append_column(sa.Column("flag", sa.Integer))
    sa.Table("audit", target_metadata, sa.Column("id", sa.Integer, primary_key=True))
    pair = DatabasePair(
        build_items_db(tmp_path / "s.db", []),
        build_items_db(tmp_path / "t.db", [], target_metadata),
    )
    try:
        report = compare_schemas(*pair.open())
    finally:
        pair.close()

    assert [(d.table, d.column, d.kind, d.source, d.target) for d in report.drifts] == [
        ("audit", None, "extra_table", None, "present"),
        ("items", "qty", "nullable", "False", "True"),
        ("items", "flag", "extra_column", None, "INTEGER [integer]"),
    ]


def test_table_filter_limits_the_comparison(pair: DatabasePair) -> None:
    report = compare_schemas(*pair.open(), tables=["items"])

    assert report.tables == ["items"]


def test_type_families_cover_the_portable_set() -> None:
    assert type_family(sa.BigInteger()) is TypeFamily.INTEGER
    assert type_family(sa.Float()) is TypeFamily.DECIMAL
    assert type_family(sa.Numeric(10, 2)) is TypeFamily.DECIMAL
    assert type_family(sa.Enum("a", "b")) is TypeFamily.TEXT
    assert type_family(sa.Date()) is TypeFamily.DATETIME
    assert type_family(sa.LargeBinary()) is TypeFamily.BINARY
    assert type_family(sa.JSON()) is TypeFamily.OTHER
