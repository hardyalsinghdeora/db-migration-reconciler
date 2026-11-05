"""Command-line entry point: argument handling only, the modules do the work."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.markup import escape
from sqlalchemy.exc import SQLAlchemyError

from reconciler import ReconcilerError, __version__
from reconciler.adapters import Database, open_database
from reconciler.backfill import run_backfill
from reconciler.checksum import Scan, run_checksum
from reconciler.counts import compare_counts
from reconciler.demo import build_demo
from reconciler.report import (
    render_backfill,
    render_checksum,
    render_counts,
    render_schema,
    write_json,
)
from reconciler.schema import compare_schemas

EXIT_OK, EXIT_DRIFT, EXIT_USAGE = 0, 1, 2

app = typer.Typer(
    help="Verify and repair a table-by-table database migration while the source stays live.",
    epilog="Exit codes: 0 no drift, 1 drift found, 2 usage or configuration error.",
    add_completion=False,
    no_args_is_help=True,
)
stdout = Console()
stderr = Console(stderr=True)

Source = Annotated[
    str, typer.Argument(metavar="SRC", help="SQLAlchemy URL or SQLite path of the live source")
]
Target = Annotated[
    str, typer.Argument(metavar="TGT", help="SQLAlchemy URL or SQLite path of the target")
]
TableName = Annotated[str, typer.Option("--table", "-t", help="Table to reconcile")]
Tables = Annotated[
    list[str] | None, typer.Option("--table", "-t", help="Only these tables (repeatable)")
]
KeyName = Annotated[str, typer.Option("--key", "-k", help="Unique integer or text key column")]
Chunk = Annotated[int, typer.Option("--chunk", min=1, help="Rows per chunk")]
Columns = Annotated[
    str | None, typer.Option("--columns", help="Comma-separated columns to compare (default: all)")
]
Where = Annotated[
    str | None,
    typer.Option("--where", help="Row filter `col op literal [AND ...]`; values are bound"),
]
MaxDiffs = Annotated[int, typer.Option("--max-diffs", min=0, help="Row diffs kept in the report")]
SleepMs = Annotated[
    int, typer.Option("--sleep-ms", min=0, help="Pause between chunks to spare a live source")
]
JsonPath = Annotated[Path | None, typer.Option("--json", help="Also write a JSON report here")]
Verbose = Annotated[bool, typer.Option("--verbose", "-v", help="Log chunks and queries to stderr")]
Write = Annotated[bool, typer.Option("--write", help="Apply changes; otherwise a dry run")]
CheckpointPath = Annotated[
    Path | None,
    typer.Option("--checkpoint", help="Resume file, updated per chunk, removed on completion"),
]


@app.callback(invoke_without_command=True)
def main(
    version: Annotated[bool, typer.Option("--version", help="Print the version and exit")] = False,
) -> None:
    if version:
        stdout.print(f"reconciler {__version__}")
        raise typer.Exit(EXIT_OK)


@app.command()
def demo(
    directory: Annotated[
        Path, typer.Option("--dir", help="Where to write the SQLite files")
    ] = Path("demo"),
    seed: Annotated[int, typer.Option(help="Seed for the generated data")] = 42,
    verbose: Verbose = False,
) -> None:
    """Create source.db and target.db with ~5,000 seeded rows and injected drift."""
    _configure_logging(verbose)
    result = build_demo(directory, seed)
    stdout.print(f"created {result.source} and {result.target}")
    for name, rows in result.rows.items():
        stdout.print(f"  {name:<12} {rows:>5} rows")
    stdout.print("injected into the target:")
    for line in result.injected:
        stdout.print(f"  - {line}")
    stdout.print(f"next: reconciler schema {result.source} {result.target}")


@app.command()
def schema(
    source: Source,
    target: Target,
    table: Tables = None,
    json_path: JsonPath = None,
    verbose: Verbose = False,
) -> None:
    """Compare tables and columns: names, portable type family, nullability."""
    with _session(verbose, source, target) as (src, tgt):
        report = compare_schemas(src, tgt, table)
        render_schema(stdout, report)
        _finish(report.ok, report.to_dict(), json_path)


@app.command()
def counts(
    source: Source,
    target: Target,
    table: Tables = None,
    where: Where = None,
    json_path: JsonPath = None,
    verbose: Verbose = False,
) -> None:
    """Count rows per table on both sides."""
    with _session(verbose, source, target) as (src, tgt):
        report = compare_counts(src, tgt, table, where)
        render_counts(stdout, report)
        _finish(report.ok, report.to_dict(), json_path)


@app.command()
def checksum(
    source: Source,
    target: Target,
    table: TableName,
    key: KeyName = "id",
    chunk: Chunk = 1000,
    columns: Columns = None,
    where: Where = None,
    max_diffs: MaxDiffs = 100,
    sleep_ms: SleepMs = 0,
    json_path: JsonPath = None,
    verbose: Verbose = False,
) -> None:
    """Compare content chunk by chunk and list the rows that differ."""
    with _session(verbose, source, target, tables=[table]) as (src, tgt):
        scan = Scan.build(src, tgt, table, key, chunk, _split(columns), where)
        report = run_checksum(src, tgt, scan, max_diffs=max_diffs, sleep_ms=sleep_ms)
        render_checksum(stdout, report)
        _finish(report.ok, report.to_dict(), json_path)


@app.command()
def backfill(
    source: Source,
    target: Target,
    table: TableName,
    key: KeyName = "id",
    chunk: Chunk = 500,
    columns: Columns = None,
    where: Where = None,
    write: Write = False,
    checkpoint: CheckpointPath = None,
    max_diffs: MaxDiffs = 100,
    sleep_ms: SleepMs = 0,
    json_path: JsonPath = None,
    verbose: Verbose = False,
) -> None:
    """Copy missing rows and overwrite changed ones; a dry run unless --write."""
    with _session(verbose, source, target, tables=[table]) as (src, tgt):
        scan = Scan.build(src, tgt, table, key, chunk, _split(columns), where)
        report = run_backfill(
            src,
            tgt,
            scan,
            write=write,
            checkpoint_path=checkpoint,
            max_diffs=max_diffs,
            sleep_ms=sleep_ms,
        )
        render_backfill(stdout, report)
        _finish(report.ok, report.to_dict(), json_path)


def _configure_logging(verbose: bool) -> None:
    # Only this package's logger is configured, and it writes through the stderr
    # console, so embedding the CLI (or testing it) never fights over the root logger.
    logger = logging.getLogger("reconciler")
    logger.handlers.clear()
    logger.addHandler(RichHandler(console=stderr, show_path=False, markup=False))
    logger.setLevel(logging.DEBUG if verbose else logging.WARNING)


@contextmanager
def _session(
    verbose: bool, *urls: str, tables: list[str] | None = None
) -> Iterator[list[Database]]:
    """Open the databases, map operator mistakes to exit code 2, always dispose."""
    _configure_logging(verbose)
    databases: list[Database] = []
    try:
        databases = [open_database(url, tables) for url in urls]
        yield databases
    except ReconcilerError as exc:
        stderr.print(f"[red]error:[/red] {escape(str(exc))}")
        raise typer.Exit(EXIT_USAGE) from None
    except SQLAlchemyError as exc:
        logging.getLogger(__name__).debug("database error", exc_info=True)
        stderr.print(f"[red]database error:[/red] {escape(str(exc))}")
        raise typer.Exit(EXIT_USAGE) from None
    finally:
        for database in databases:
            database.dispose()


def _finish(ok: bool, payload: dict[str, Any], json_path: Path | None) -> None:
    if json_path is not None:
        write_json(json_path, payload)
        stdout.print(f"report written to {json_path}")
    raise typer.Exit(EXIT_OK if ok else EXIT_DRIFT)


def _split(columns: str | None) -> list[str] | None:
    return [c.strip() for c in columns.split(",") if c.strip()] if columns else None
