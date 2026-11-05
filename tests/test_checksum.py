import datetime as dt
import random
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from reconciler import ReconcilerError
from reconciler.adapters import Database
from reconciler.checksum import Scan, chunk_digest, normalise, run_checksum
from tests.conftest import DatabasePair, build_items_db, item_rows


def _scan(pair: DatabasePair, chunk: int = 10, **options: Any) -> tuple[Database, Database, Scan]:
    source, target = pair.open(["items"])
    return source, target, Scan.build(source, target, "items", "id", chunk, **options)


def _drifted(tmp_path: Path, mutate: Any) -> DatabasePair:
    rows = item_rows()
    target_rows = [dict(row) for row in rows]
    mutate(target_rows)
    return DatabasePair(
        build_items_db(tmp_path / "s.db", rows), build_items_db(tmp_path / "t.db", target_rows)
    )


def test_identical_tables_have_no_mismatch(pair: DatabasePair) -> None:
    report = run_checksum(*_scan(pair))

    assert report.ok
    assert [chunk.match for chunk in report.chunks] == [True, True, True]
    assert report.tally.counts == {"missing": 0, "extra": 0, "changed": 0}
    assert (report.source_rows, report.target_rows) == (25, 25)


def test_changed_row_reports_exactly_that_key_and_column(tmp_path: Path) -> None:
    def mutate(rows: list[dict[str, Any]]) -> None:
        rows[11]["qty"] = 999  # id 12 lives in the second chunk

    pair = _drifted(tmp_path, mutate)
    try:
        report = run_checksum(*_scan(pair))
    finally:
        pair.close()

    assert [chunk.index for chunk in report.mismatched] == [1]
    assert [(d.key, d.kind, d.columns) for d in report.tally.diffs] == [
        (12, "changed", {"qty": (36, 999)})
    ]


def test_missing_row_in_target_is_reported(tmp_path: Path) -> None:
    pair = _drifted(tmp_path, lambda rows: rows.pop(2))  # id 3
    try:
        report = run_checksum(*_scan(pair))
    finally:
        pair.close()

    assert [(d.key, d.kind) for d in report.tally.diffs] == [(3, "missing")]
    assert report.tally.counts["missing"] == 1
    assert report.chunks[0].target_rows == 9


def test_extra_rows_in_target_are_reported_inside_and_past_the_source_range(
    tmp_path: Path,
) -> None:
    def mutate(rows: list[dict[str, Any]]) -> None:
        rows.append(dict(rows[0], id=100))

    pair = _drifted(tmp_path, mutate)
    try:
        source, target, scan = _scan(pair)
        with source.engine.begin() as connection:
            connection.execute(
                source.table("items").delete().where(source.table("items").c.id == 7)
            )
        report = run_checksum(source, target, scan)
    finally:
        pair.close()

    assert [(d.key, d.kind) for d in report.tally.diffs] == [(7, "extra"), (100, "extra")]
    assert report.target_rows == 26


def test_chunk_boundaries_when_row_count_is_not_a_multiple_of_chunk_size(
    pair: DatabasePair,
) -> None:
    report = run_checksum(*_scan(pair, chunk=10))

    assert [(c.index, c.lo, c.hi, c.source_rows) for c in report.chunks] == [
        (0, None, 10, 10),
        (1, 10, 20, 10),
        (2, 20, 25, 5),
    ]


def test_exact_multiple_does_not_produce_an_empty_trailing_chunk(pair: DatabasePair) -> None:
    report = run_checksum(*_scan(pair, chunk=5))

    assert len(report.chunks) == 5
    assert report.chunks[-1].hi == 25


def test_digest_is_order_independent_and_content_sensitive() -> None:
    rows = item_rows(50)
    columns = list(rows[0])
    shuffled = rows[:]
    random.Random(7).shuffle(shuffled)

    assert chunk_digest(rows, columns) == chunk_digest(shuffled, columns)
    assert chunk_digest(rows, columns) != chunk_digest(rows[:-1], columns)
    assert chunk_digest(rows, columns) != chunk_digest([dict(rows[0], qty=1), *rows[1:]], columns)


def test_rows_inserted_in_a_different_order_give_the_same_digest(tmp_path: Path) -> None:
    rows = item_rows()
    pair = DatabasePair(
        build_items_db(tmp_path / "s.db", rows),
        build_items_db(tmp_path / "t.db", list(reversed(rows))),
    )
    try:
        report = run_checksum(*_scan(pair, chunk=25))
    finally:
        pair.close()

    assert report.ok
    assert report.chunks[0].source_digest == report.chunks[0].target_digest


def test_normalise_maps_engine_specific_values_onto_one_representation() -> None:
    aware = dt.datetime(2024, 5, 6, 7, 8, 9, tzinfo=dt.timezone(dt.timedelta(hours=2)))

    assert normalise(True) == 1 and normalise(False) == 0  # MySQL TINYINT(1) vs BOOLEAN
    assert normalise(Decimal("10.50")) == normalise(10.5) == "10.5"
    assert normalise(Decimal("100.00")) == normalise(100.0) == 100
    assert normalise(aware) == "2024-05-06T05:08:09.000000"
    assert normalise(dt.datetime(2024, 5, 6, 5, 8, 9)) == normalise(aware)
    assert normalise(b"\x00\xff") == "00ff"
    assert normalise("100") != normalise(100)


def test_where_predicate_restricts_both_sides(pair: DatabasePair) -> None:
    report = run_checksum(*_scan(pair, chunk=10, where="active = TRUE AND note IS NOT NULL"))

    assert report.ok
    assert report.source_rows == report.target_rows == 10


def test_scan_build_rejects_bad_key_columns_and_predicates(pair: DatabasePair) -> None:
    source, target = pair.open(["items"])

    with pytest.raises(ReconcilerError, match="key column 'nope' not found"):
        Scan.build(source, target, "items", "nope", 10)
    with pytest.raises(ReconcilerError, match="must be an integer or text"):
        Scan.build(source, target, "items", "created", 10)
    with pytest.raises(ReconcilerError, match="column 'ghost' not found"):
        Scan.build(source, target, "items", "id", 10, columns=["ghost"])
    with pytest.raises(ReconcilerError, match="--where"):
        Scan.build(source, target, "items", "id", 10, where="qty > 1 OR qty < 0")
    with pytest.raises(ReconcilerError, match="chunk size"):
        Scan.build(source, target, "items", "id", 0)


def test_columns_option_limits_the_comparison_and_always_includes_the_key(
    tmp_path: Path,
) -> None:
    def mutate(rows: list[dict[str, Any]]) -> None:
        rows[0]["note"] = "changed elsewhere"

    pair = _drifted(tmp_path, mutate)
    try:
        source, target, scan = _scan(pair, columns=["qty"])
        report = run_checksum(source, target, scan)
    finally:
        pair.close()

    assert scan.columns == ["id", "qty"]
    assert report.ok


def test_max_diffs_bounds_the_details_but_not_the_counts(tmp_path: Path) -> None:
    pair = DatabasePair(
        build_items_db(tmp_path / "s.db", item_rows()), build_items_db(tmp_path / "t.db", [])
    )
    try:
        report = run_checksum(*_scan(pair, chunk=10), max_diffs=4)
    finally:
        pair.close()

    assert len(report.tally.diffs) == 4
    assert report.tally.truncated
    assert report.tally.counts["missing"] == 25
    assert report.to_dict()["summary"]["truncated"] is True
