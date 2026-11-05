"""Copy missing rows and overwrite changed ones, in resumable keyset chunks.

Target rows that the source does not have are reported, never deleted: on a
live source a row can be missing because it was just deleted, but on the
target it may also be the only copy of something an application already
wrote there. Deletion is a human decision.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from reconciler import checkpoint as checkpoints
from reconciler.adapters import Database
from reconciler.checksum import DiffTally, Key, Scan, diff_rows, fetch, iter_chunks, tail_extras
from reconciler.dialects import upsert

log = logging.getLogger(__name__)


@dataclass
class BackfillReport:
    scan: Scan
    dry_run: bool
    tally: DiffTally
    resumed_after: Key | None = None
    chunks: int = 0
    scanned: int = 0
    written: int = 0

    @property
    def ok(self) -> bool:
        """True when the target needs nothing more from this tool."""
        counts = self.tally.counts
        unrepaired = counts["missing"] + counts["changed"] if self.dry_run else 0
        return unrepaired == 0 and counts["extra"] == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "table": self.scan.table,
            "key": self.scan.key,
            "columns": self.scan.columns,
            "chunk_size": self.scan.chunk_size,
            "where": self.scan.where,
            "dry_run": self.dry_run,
            "resumed_after": self.resumed_after,
            "summary": {
                "ok": self.ok,
                "chunks": self.chunks,
                "scanned": self.scanned,
                "written": self.written,
                **self.tally.counts,
                "truncated": self.tally.truncated,
            },
            "diffs": [diff.to_dict() for diff in self.tally.diffs],
        }


ChunkHook = Callable[[BackfillReport], None]


def run_backfill(
    source: Database,
    target: Database,
    scan: Scan,
    *,
    write: bool = False,
    checkpoint_path: Path | None = None,
    max_diffs: int = 100,
    sleep_ms: int = 0,
    on_chunk: ChunkHook | None = None,
) -> BackfillReport:
    """Reconcile ``scan.table`` chunk by chunk; ``on_chunk`` runs after each committed chunk."""
    target_table = target.table(scan.table)
    statement = upsert(target.engine.dialect, target_table, scan.key, scan.columns)
    resume = checkpoints.load(checkpoint_path, scan.table, scan.key) if checkpoint_path else None
    report = BackfillReport(scan, not write, DiffTally(max_diffs))
    written_before = 0
    if resume is not None:
        report.resumed_after, written_before = resume.last_key, resume.rows_written
        log.info("resuming after key=%s (%d chunks done)", resume.last_key, resume.chunks_done)
    last_key = report.resumed_after
    for _, lo, source_rows in iter_chunks(source, scan, report.resumed_after, sleep_ms):
        hi = source_rows[-1][scan.key]
        target_rows = fetch(target, target_table, scan, lo, hi, None)
        diffs = diff_rows(source_rows, target_rows, scan)
        stale = {diff.key for diff in diffs if diff.kind != "extra"}
        rows = [{c: row[c] for c in scan.columns} for row in source_rows if row[scan.key] in stale]
        if write and rows:
            with target.engine.begin() as connection:
                connection.execute(statement, rows)
            report.written += len(rows)
        report.chunks += 1
        report.scanned += len(source_rows)
        report.tally.record(diffs)
        last_key = hi
        log.info(
            "chunk lo=%s hi=%s scanned=%d stale=%d written=%d dry_run=%s",
            lo,
            hi,
            len(source_rows),
            len(rows),
            len(rows) if write else 0,
            not write,
        )
        if write and checkpoint_path:
            # The upsert above is idempotent, so a crash between the commit and
            # this write only costs one repeated chunk on resume.
            progress = checkpoints.Checkpoint.now(
                scan.table, scan.key, hi, report.chunks, written_before + report.written
            )
            checkpoints.save(checkpoint_path, progress)
        if on_chunk is not None:
            on_chunk(report)
    report.tally.record(tail_extras(target, scan, last_key, max_diffs))
    if write and checkpoint_path:
        checkpoints.clear(checkpoint_path)
        log.info("run complete; checkpoint %s removed", checkpoint_path)
    return report
