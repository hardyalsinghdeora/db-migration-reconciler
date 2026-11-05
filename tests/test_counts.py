from pathlib import Path

import sqlalchemy as sa

from reconciler.counts import compare_counts, count_rows
from tests.conftest import DatabasePair, build_items_db, item_rows


def test_equal_counts_are_ok(pair: DatabasePair) -> None:
    report = compare_counts(*pair.open())

    assert report.ok
    assert report.to_dict()["tables"] == [
        {"table": "items", "source": 25, "target": 25, "difference": 0}
    ]


def test_mismatch_is_reported_with_signed_difference(tmp_path: Path) -> None:
    pair = DatabasePair(
        build_items_db(tmp_path / "s.db", item_rows(10)),
        build_items_db(tmp_path / "t.db", item_rows(7)),
    )
    try:
        report = compare_counts(*pair.open())
    finally:
        pair.close()

    assert not report.ok
    assert report.results[0].difference == -3


def test_missing_target_table_counts_as_mismatch(tmp_path: Path) -> None:
    empty = tmp_path / "t.db"
    engine = sa.create_engine(f"sqlite:///{empty}")
    sa.MetaData().create_all(engine)
    engine.dispose()
    pair = DatabasePair(build_items_db(tmp_path / "s.db", item_rows(2)), str(empty))
    try:
        report = compare_counts(*pair.open())
    finally:
        pair.close()

    assert not report.ok
    assert report.results[0].target is None
    assert report.results[0].difference is None


def test_where_predicate_filters_both_sides(pair: DatabasePair) -> None:
    source, target = pair.open()

    assert count_rows(source, source.table("items"), "active = TRUE AND qty > 30") == 7
    assert compare_counts(source, target, where="note IS NULL").results[0].source == 5
