import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Dialect
from sqlalchemy.dialects import mysql, oracle, postgresql, sqlite

from reconciler import ReconcilerError
from reconciler import checkpoint as checkpoints
from reconciler.adapters import Database
from reconciler.backfill import BackfillReport, run_backfill
from reconciler.checksum import Scan, run_checksum
from reconciler.dialects import upsert
from tests.conftest import DatabasePair, build_items_db, item_metadata, item_rows, read_items

EXTRA_KEY = 100


@pytest.fixture
def drifted(tmp_path: Path) -> Any:
    """Target is missing ids 3 and 17, has id 12 changed, and holds an extra id 100."""
    rows = item_rows()
    target_rows = [dict(row) for row in rows if row["id"] not in (3, 17)]
    target_rows[10]["qty"] = 999  # id 12
    target_rows.append(dict(rows[0], id=EXTRA_KEY))
    pair = DatabasePair(
        build_items_db(tmp_path / "s.db", rows), build_items_db(tmp_path / "t.db", target_rows)
    )
    yield pair
    pair.close()


def _scan(pair: DatabasePair, chunk: int = 5) -> tuple[Database, Database, Scan]:
    source, target = pair.open(["items"])
    return source, target, Scan.build(source, target, "items", "id", chunk)


def _target_matches_source(pair: DatabasePair) -> bool:
    target = read_items(pair.target_path)
    target.pop(EXTRA_KEY, None)
    return target == read_items(pair.source_path)


def test_dry_run_reports_but_changes_nothing(drifted: DatabasePair) -> None:
    before = read_items(drifted.target_path)

    report = run_backfill(*_scan(drifted))

    assert report.dry_run and not report.ok
    assert report.tally.counts == {"missing": 2, "changed": 1, "extra": 1}
    assert report.written == 0
    assert read_items(drifted.target_path) == before


def test_write_repairs_missing_and_changed_rows_but_never_deletes_extras(
    drifted: DatabasePair,
) -> None:
    report = run_backfill(*_scan(drifted), write=True)

    assert report.written == 3
    assert _target_matches_source(drifted)
    assert EXTRA_KEY in read_items(drifted.target_path)
    assert not report.ok  # the extra row is drift this tool refuses to repair
    assert report.to_dict()["summary"]["extra"] == 1


def test_rerun_after_completion_is_a_noop(drifted: DatabasePair) -> None:
    run_backfill(*_scan(drifted), write=True)
    snapshot = read_items(drifted.target_path)

    again = run_backfill(*_scan(drifted), write=True)

    assert (again.written, again.tally.counts["missing"], again.tally.counts["changed"]) == (
        0,
        0,
        0,
    )
    assert again.scanned == 25
    assert read_items(drifted.target_path) == snapshot


def test_interrupted_run_resumes_from_the_checkpoint_without_rereading(
    drifted: DatabasePair, tmp_path: Path
) -> None:
    checkpoint = tmp_path / "checkpoint.json"

    def crash_after_two_chunks(report: BackfillReport) -> None:
        if report.chunks == 2:
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError, match="simulated crash"):
        run_backfill(
            *_scan(drifted), write=True, checkpoint_path=checkpoint, on_chunk=crash_after_two_chunks
        )
    saved = checkpoints.load(checkpoint, "items", "id")
    assert saved is not None
    assert (saved.last_key, saved.chunks_done, saved.rows_written) == (10, 2, 1)  # id 3 fixed

    resumed = run_backfill(*_scan(drifted), write=True, checkpoint_path=checkpoint)

    assert resumed.resumed_after == 10
    assert (resumed.chunks, resumed.scanned) == (3, 15)  # chunks 1 and 2 were not read again
    assert resumed.written == 2  # ids 12 and 17
    assert not checkpoint.exists()
    assert _target_matches_source(drifted)
    assert run_checksum(*_scan(drifted)).tally.counts == {"missing": 0, "changed": 0, "extra": 1}


def test_dry_run_never_writes_a_checkpoint(drifted: DatabasePair, tmp_path: Path) -> None:
    checkpoint = tmp_path / "checkpoint.json"

    run_backfill(*_scan(drifted), checkpoint_path=checkpoint)

    assert not checkpoint.exists()


def test_checkpoint_for_another_table_or_key_is_refused(
    drifted: DatabasePair, tmp_path: Path
) -> None:
    checkpoint = tmp_path / "checkpoint.json"
    checkpoints.save(checkpoint, checkpoints.Checkpoint.now("orders", "id", 10, 1, 0))

    with pytest.raises(ReconcilerError, match="belongs to table 'orders'"):
        run_backfill(*_scan(drifted), write=True, checkpoint_path=checkpoint)


def test_corrupt_checkpoint_is_an_error_not_a_silent_restart(tmp_path: Path) -> None:
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text(json.dumps({"table": "items"}))

    with pytest.raises(ReconcilerError, match="not readable"):
        checkpoints.load(checkpoint, "items", "id")


def test_upsert_statement_follows_the_dialect() -> None:
    table = item_metadata().tables["items"]

    def sql(module: Any, columns: Sequence[str] = ("id", "name", "qty")) -> str:
        dialect: Dialect = module.dialect()
        return str(upsert(dialect, table, "id", columns).compile(dialect=dialect))

    assert "ON CONFLICT (id) DO UPDATE SET name = excluded.name" in sql(sqlite)
    assert "ON CONFLICT (id) DO UPDATE SET name = excluded.name" in sql(postgresql)
    assert "ON DUPLICATE KEY UPDATE name = VALUES(name)" in sql(mysql)
    assert "DO NOTHING" in sql(sqlite, ["id"])
    with pytest.raises(ReconcilerError, match="not implemented for dialect 'oracle'"):
        sql(oracle)
