"""Rich tables for people and JSON files for machines."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from reconciler.backfill import BackfillReport
from reconciler.checksum import ChecksumReport, RowDiff
from reconciler.counts import CountReport
from reconciler.schema import SchemaReport

_SHOWN_CHUNKS = 20


def render_schema(console: Console, report: SchemaReport) -> None:
    if report.ok:
        console.print(f"[green]schema OK[/green]: {len(report.tables)} table(s) match")
        return
    table = _table("Schema drift", "Table", "Column", "Drift", "Source", "Target")
    for drift in report.drifts:
        table.add_row(
            drift.table,
            drift.column or "-",
            drift.kind,
            escape(drift.source or "-"),
            escape(drift.target or "-"),
        )
    console.print(table)
    console.print(f"[red]{len(report.drifts)} drift(s)[/red] across {len(report.tables)} table(s)")


def render_counts(console: Console, report: CountReport) -> None:
    table = _table("Row counts", "Table", "Source", "Target", "Difference", "Status")
    for result in report.results:
        table.add_row(
            result.table,
            str(result.source),
            "missing" if result.target is None else str(result.target),
            "-" if result.difference is None else f"{result.difference:+d}",
            "[green]ok[/green]" if result.ok else "[red]MISMATCH[/red]",
        )
    console.print(table)
    mismatched = sum(1 for result in report.results if not result.ok)
    _verdict(console, report.ok, f"{mismatched} of {len(report.results)} table(s) differ")


def render_checksum(console: Console, report: ChecksumReport) -> None:
    scan = report.scan
    console.print(_heading(scan.table, scan.key, scan.chunk_size, len(scan.columns), scan.where))
    if report.mismatched:
        table = _table("Mismatched chunks", "Chunk", "Range", "Source", "Target", "Digests")
        for chunk in report.mismatched[:_SHOWN_CHUNKS]:
            table.add_row(
                str(chunk.index),
                f"({chunk.lo}, {chunk.hi}]",
                str(chunk.source_rows),
                str(chunk.target_rows),
                f"{chunk.source_digest[:12]} vs {chunk.target_digest[:12]}",
            )
        console.print(table)
        if len(report.mismatched) > _SHOWN_CHUNKS:
            console.print(
                f"  ... {len(report.mismatched) - _SHOWN_CHUNKS} more mismatched chunk(s)"
            )
    _render_diffs(console, report.tally.diffs, report.tally.truncated, "Differing rows")
    counts = report.tally.counts
    _verdict(
        console,
        report.ok,
        f"{len(report.chunks)} chunk(s), {len(report.mismatched)} mismatched; "
        f"source rows {report.source_rows}, target rows {report.target_rows}; "
        f"missing {counts['missing']}, changed {counts['changed']}, extra {counts['extra']}",
    )


def render_backfill(console: Console, report: BackfillReport) -> None:
    scan = report.scan
    mode = "[yellow]DRY RUN[/yellow]" if report.dry_run else "[bold]WRITE[/bold]"
    heading = _heading(scan.table, scan.key, scan.chunk_size, len(scan.columns), scan.where)
    console.print(f"{heading}  mode={mode}")
    if report.resumed_after is not None:
        console.print(f"resumed after key {report.resumed_after}")
    title = "Rows that would change" if report.dry_run else "Rows reconciled"
    _render_diffs(console, report.tally.diffs, report.tally.truncated, title)
    counts = report.tally.counts
    verb = "would write" if report.dry_run else "wrote"
    _verdict(
        console,
        report.ok,
        f"scanned {report.scanned} row(s) in {report.chunks} chunk(s); "
        f"missing {counts['missing']}, changed {counts['changed']}, "
        f"{verb} {counts['missing'] + counts['changed'] if report.dry_run else report.written}; "
        f"extra {counts['extra']} (left in place, never deleted)",
    )


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, default=_json_default) + "\n")


def _json_default(value: object) -> object:
    if isinstance(value, bytes | bytearray | memoryview):
        return bytes(value).hex()
    return str(value)  # Decimal, datetime, UUID: readable and unambiguous


def _table(title: str, *columns: str) -> Table:
    table = Table(title=title, title_justify="left")
    for column in columns:
        table.add_column(column)
    return table


def _heading(table: str, key: str, chunk: int, columns: int, where: str | None) -> str:
    text = f"[bold]{table}[/bold] key={key} chunk={chunk} columns={columns}"
    return f"{text} where={escape(repr(where))}" if where else text


def _render_diffs(console: Console, diffs: Iterable[RowDiff], truncated: bool, title: str) -> None:
    table = _table(title, "Key", "Kind", "Column", "Source", "Target")
    for diff in diffs:
        if diff.columns:
            for column, (source, target) in diff.columns.items():
                table.add_row(_cell(diff.key), diff.kind, column, _cell(source), _cell(target))
        else:
            table.add_row(_cell(diff.key), diff.kind, "-", "-", "-")
    if table.row_count:
        console.print(table)
    if truncated:
        console.print("  ... more differences not listed; raise --max-diffs to see them")


def _cell(value: object) -> str:
    # Data goes through escape() so a value like "[bold]" is shown, not interpreted.
    return "NULL" if value is None else escape(str(value))


def _verdict(console: Console, ok: bool, summary: str) -> None:
    status = "[green]OK[/green]" if ok else "[red]DRIFT[/red]"
    console.print(f"{status}: {summary}")
