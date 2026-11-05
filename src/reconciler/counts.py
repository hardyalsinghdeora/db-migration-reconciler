"""Row counts per table: the cheapest signal, so it runs before any checksum."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Table, func, select

from reconciler.adapters import Database
from reconciler.predicate import parse_where


@dataclass(frozen=True)
class CountResult:
    table: str
    source: int
    target: int | None  # None when the table does not exist in the target

    @property
    def difference(self) -> int | None:
        return None if self.target is None else self.target - self.source

    @property
    def ok(self) -> bool:
        return self.target == self.source


@dataclass(frozen=True)
class CountReport:
    results: list[CountResult]
    where: str | None

    @property
    def ok(self) -> bool:
        return all(result.ok for result in self.results)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "where": self.where,
            "tables": [
                {
                    "table": r.table,
                    "source": r.source,
                    "target": r.target,
                    "difference": r.difference,
                }
                for r in self.results
            ],
        }


def count_rows(database: Database, table: Table, where: str | None = None) -> int:
    statement = select(func.count()).select_from(table)
    if where:
        statement = statement.where(parse_where(where, table))
    with database.engine.connect() as connection:
        return int(connection.execute(statement).scalar_one())


def compare_counts(
    source: Database,
    target: Database,
    tables: Sequence[str] | None = None,
    where: str | None = None,
) -> CountReport:
    """Count every source table (or ``tables``) on both sides."""
    results: list[CountResult] = []
    for name in tables or source.table_names():
        source_count = count_rows(source, source.table(name), where)
        target_table = target.metadata.tables.get(name)
        target_count = None if target_table is None else count_rows(target, target_table, where)
        results.append(CountResult(name, source_count, target_count))
    return CountReport(results, where)
