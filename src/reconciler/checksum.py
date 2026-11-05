"""Chunked, order-independent content comparison with drill-down diffs.

The source key space is walked with keyset pagination; each chunk's key range
is then read from the target so both sides are compared over identical ranges,
and a missing row never shifts the boundaries of every later chunk.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import time
import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import RowMapping, Select, Table, select

from reconciler import ReconcilerError
from reconciler.adapters import Database
from reconciler.dialects import TypeFamily, render_sql, type_family
from reconciler.predicate import parse_where

log = logging.getLogger(__name__)

Key = int | str
Row = Mapping[Any, Any]  # a RowMapping or a plain dict keyed by column name

_DIGEST_MASK = (1 << 256) - 1


def normalise(value: object) -> object:
    """Map driver-specific Python values onto one portable representation.

    The rules follow what a cross-engine migration must tolerate: MySQL hands
    back TINYINT(1) booleans as ints, DECIMAL and FLOAT columns disagree on
    trailing zeros, and PostgreSQL returns timezone-aware timestamps.
    """
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float | Decimal):
        return _number(value)
    if isinstance(value, dt.datetime):
        if value.tzinfo is not None:
            value = value.astimezone(dt.UTC).replace(tzinfo=None)
        return value.isoformat(timespec="microseconds")
    if isinstance(value, dt.date | dt.time):
        return value.isoformat()
    if isinstance(value, bytes | bytearray | memoryview):
        return bytes(value).hex()
    if isinstance(value, uuid.UUID):
        return str(value)
    return str(value)


def _number(value: float | Decimal) -> object:
    decimal = value if isinstance(value, Decimal) else Decimal(repr(value))
    if not decimal.is_finite():
        return str(decimal)
    if decimal == decimal.to_integral_value():
        return int(decimal)
    return format(decimal.normalize(), "f")


def canonical_row(row: Row, columns: Sequence[str]) -> str:
    return json.dumps([normalise(row[column]) for column in columns], separators=(",", ":"))


def row_hash(row: Row, columns: Sequence[str]) -> int:
    return int.from_bytes(hashlib.sha256(canonical_row(row, columns).encode()).digest(), "big")


def chunk_digest(rows: Iterable[Row], columns: Sequence[str]) -> str:
    """Sum of row hashes modulo 2**256: independent of row order, sensitive to any row."""
    total = 0
    for row in rows:
        total = (total + row_hash(row, columns)) & _DIGEST_MASK
    return f"{total:064x}"


@dataclass(frozen=True)
class Scan:
    """One table, one key, one column list: everything a chunked read needs."""

    table: str
    key: str
    columns: list[str]
    chunk_size: int
    where: str | None = None

    @classmethod
    def build(
        cls,
        source: Database,
        target: Database,
        table: str,
        key: str,
        chunk_size: int,
        columns: Sequence[str] | None = None,
        where: str | None = None,
    ) -> Scan:
        """Validate key, columns and predicate against both sides before any read."""
        source_table, target_table = source.table(table), target.table(table)
        if chunk_size < 1:
            raise ReconcilerError("chunk size must be at least 1")
        for side, reflected in (("source", source_table), ("target", target_table)):
            if key not in reflected.c:
                raise ReconcilerError(f"key column {key!r} not found in {side} table {table!r}")
        if type_family(source_table.c[key].type) not in {TypeFamily.INTEGER, TypeFamily.TEXT}:
            raise ReconcilerError(f"key column {key!r} must be an integer or text column")
        if key not in {c.name for c in source_table.primary_key}:
            log.warning("key %s.%s is not the primary key; results assume it is unique", table, key)
        wanted = list(columns) if columns else [c.name for c in source_table.columns]
        if key not in wanted:
            wanted.insert(0, key)
        for column in wanted:
            for side, reflected in (("source", source_table), ("target", target_table)):
                if column not in reflected.c:
                    raise ReconcilerError(
                        f"column {column!r} not found in {side} table {table!r}; "
                        "use --columns to compare a subset"
                    )
        if where:
            parse_where(where, source_table)
            parse_where(where, target_table)
        return cls(table, key, wanted, chunk_size, where)

    def select(
        self,
        table: Table,
        lo: Key | None,
        hi: Key | None,
        limit: int | None,
        columns: Sequence[str] | None = None,
    ) -> Select[Any]:
        """``WHERE key > :lo [AND key <= :hi] ORDER BY key [LIMIT n]``: keyset, never OFFSET."""
        key = table.c[self.key]
        statement = select(*(table.c[c] for c in columns or self.columns)).order_by(key)
        if lo is not None:
            statement = statement.where(key > lo)
        if hi is not None:
            statement = statement.where(key <= hi)
        if self.where:
            statement = statement.where(parse_where(self.where, table))
        return statement if limit is None else statement.limit(limit)


def fetch(
    database: Database,
    table: Table,
    scan: Scan,
    lo: Key | None,
    hi: Key | None,
    limit: int | None,
    columns: Sequence[str] | None = None,
) -> list[RowMapping]:
    """Read one key range in its own short transaction."""
    statement = scan.select(table, lo, hi, limit, columns)
    if log.isEnabledFor(logging.DEBUG):
        log.debug("%s: %s", database.label, render_sql(statement, database.engine.dialect))
    with database.engine.connect() as connection:
        return list(connection.execute(statement).mappings())


def iter_chunks(
    source: Database, scan: Scan, start_after: Key | None = None, sleep_ms: int = 0
) -> Iterator[tuple[int, Key | None, list[RowMapping]]]:
    """Yield ``(index, lo, rows)`` for each source chunk; ``lo`` is exclusive."""
    table = source.table(scan.table)
    lo, index = start_after, 0
    while True:
        rows = fetch(source, table, scan, lo, None, scan.chunk_size)
        if not rows:
            return
        yield index, lo, rows
        if len(rows) < scan.chunk_size:
            return
        lo, index = rows[-1][scan.key], index + 1
        if sleep_ms:
            time.sleep(sleep_ms / 1000)


@dataclass(frozen=True)
class ChunkResult:
    index: int
    lo: Key | None  # exclusive
    hi: Key  # inclusive
    source_rows: int
    target_rows: int
    source_digest: str
    target_digest: str

    @property
    def match(self) -> bool:
        return self.source_digest == self.target_digest


@dataclass(frozen=True)
class RowDiff:
    key: Key
    kind: str  # missing | extra | changed
    columns: dict[str, tuple[object, object]]  # column -> (source value, target value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "kind": self.kind,
            "columns": {c: {"source": s, "target": t} for c, (s, t) in self.columns.items()},
        }


def diff_rows(source_rows: Sequence[Row], target_rows: Sequence[Row], scan: Scan) -> list[RowDiff]:
    """Compare two sets of rows over the same key range, by normalised value."""
    source = {row[scan.key]: row for row in source_rows}
    target = {row[scan.key]: row for row in target_rows}
    diffs: list[RowDiff] = []
    for key in sorted(source.keys() | target.keys(), key=lambda k: (type(k).__name__, k)):
        s, t = source.get(key), target.get(key)
        if t is None:
            diffs.append(RowDiff(key, "missing", {}))
        elif s is None:
            diffs.append(RowDiff(key, "extra", {}))
        else:
            changed = {c: (s[c], t[c]) for c in scan.columns if normalise(s[c]) != normalise(t[c])}
            if changed:
                diffs.append(RowDiff(key, "changed", changed))
    return diffs


@dataclass
class DiffTally:
    """Counts every difference but keeps only the first ``max_diffs`` details."""

    max_diffs: int
    counts: dict[str, int] = field(default_factory=lambda: {"missing": 0, "extra": 0, "changed": 0})
    diffs: list[RowDiff] = field(default_factory=list)
    truncated: bool = False

    def record(self, diffs: Iterable[RowDiff]) -> None:
        for diff in diffs:
            self.counts[diff.kind] += 1
            if len(self.diffs) < self.max_diffs:
                self.diffs.append(diff)
            else:
                self.truncated = True


@dataclass
class ChecksumReport:
    scan: Scan
    tally: DiffTally
    chunks: list[ChunkResult] = field(default_factory=list)
    source_rows: int = 0
    target_rows: int = 0

    @property
    def mismatched(self) -> list[ChunkResult]:
        return [chunk for chunk in self.chunks if not chunk.match]

    @property
    def ok(self) -> bool:
        return not self.mismatched and not any(self.tally.counts.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "table": self.scan.table,
            "key": self.scan.key,
            "columns": self.scan.columns,
            "chunk_size": self.scan.chunk_size,
            "where": self.scan.where,
            "summary": {
                "ok": self.ok,
                "chunks": len(self.chunks),
                "mismatched_chunks": len(self.mismatched),
                "source_rows": self.source_rows,
                "target_rows": self.target_rows,
                **self.tally.counts,
                "truncated": self.tally.truncated,
            },
            "chunks": [asdict(chunk) | {"match": chunk.match} for chunk in self.chunks],
            "diffs": [diff.to_dict() for diff in self.tally.diffs],
        }


def tail_extras(target: Database, scan: Scan, after: Key | None, limit: int) -> list[RowDiff]:
    """Rows past the source's last key can only be extras; count them, list at most ``limit``."""
    table = target.table(scan.table)
    keys = fetch(target, table, scan, after, None, limit + 1, columns=[scan.key])
    return [RowDiff(row[scan.key], "extra", {}) for row in keys]


def run_checksum(
    source: Database, target: Database, scan: Scan, *, max_diffs: int = 100, sleep_ms: int = 0
) -> ChecksumReport:
    """Compare ``scan.table`` chunk by chunk and drill into every mismatched chunk."""
    target_table = target.table(scan.table)
    report = ChecksumReport(scan, DiffTally(max_diffs))
    last_key: Key | None = None
    for index, lo, source_rows in iter_chunks(source, scan, sleep_ms=sleep_ms):
        hi = source_rows[-1][scan.key]
        target_rows = fetch(target, target_table, scan, lo, hi, None)
        chunk = ChunkResult(
            index,
            lo,
            hi,
            len(source_rows),
            len(target_rows),
            chunk_digest(source_rows, scan.columns),
            chunk_digest(target_rows, scan.columns),
        )
        report.chunks.append(chunk)
        report.source_rows += len(source_rows)
        report.target_rows += len(target_rows)
        log.info(
            "chunk=%d lo=%s hi=%s source_rows=%d target_rows=%d match=%s",
            index,
            lo,
            hi,
            len(source_rows),
            len(target_rows),
            chunk.match,
        )
        if not chunk.match:
            report.tally.record(diff_rows(source_rows, target_rows, scan))
        last_key = hi
    extras = tail_extras(target, scan, last_key, max_diffs)
    report.target_rows += len(extras)
    report.tally.record(extras)
    return report
