"""Compare reflected schemas at the level that matters for a migration."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import Table

from reconciler.adapters import Database
from reconciler.dialects import TypeFamily, type_family


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    family: TypeFamily
    nullable: bool
    type_name: str

    @property
    def describe(self) -> str:
        return f"{self.type_name} [{self.family}]"


@dataclass(frozen=True)
class SchemaDrift:
    """One difference; ``kind`` is one of missing_table, extra_table, missing_column,
    extra_column, type or nullable."""

    table: str
    column: str | None
    kind: str
    source: str | None
    target: str | None


@dataclass(frozen=True)
class SchemaReport:
    tables: list[str]
    drifts: list[SchemaDrift]

    @property
    def ok(self) -> bool:
        return not self.drifts

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "tables": self.tables,
            "drifts": [asdict(drift) for drift in self.drifts],
        }


def describe_table(table: Table) -> dict[str, ColumnInfo]:
    return {
        column.name: ColumnInfo(
            column.name, type_family(column.type), bool(column.nullable), str(column.type)
        )
        for column in table.columns
    }


def compare_schemas(
    source: Database, target: Database, tables: Sequence[str] | None = None
) -> SchemaReport:
    """Report every table/column difference, in a stable order."""
    names = list(tables or []) or sorted(set(source.table_names()) | set(target.table_names()))
    drifts: list[SchemaDrift] = []
    for name in names:
        source_table = source.metadata.tables.get(name)
        target_table = target.metadata.tables.get(name)
        if source_table is None:
            drifts.append(SchemaDrift(name, None, "extra_table", None, "present"))
        elif target_table is None:
            drifts.append(SchemaDrift(name, None, "missing_table", "present", None))
        else:
            drifts.extend(
                _compare_columns(name, describe_table(source_table), describe_table(target_table))
            )
    return SchemaReport(names, drifts)


def _compare_columns(
    table: str, source: dict[str, ColumnInfo], target: dict[str, ColumnInfo]
) -> Iterator[SchemaDrift]:
    for name, column in source.items():
        other = target.get(name)
        if other is None:
            yield SchemaDrift(table, name, "missing_column", column.describe, None)
            continue
        if column.family != other.family:
            yield SchemaDrift(table, name, "type", column.describe, other.describe)
        if column.nullable != other.nullable:
            yield SchemaDrift(table, name, "nullable", str(column.nullable), str(other.nullable))
    for name in sorted(target.keys() - source.keys()):
        yield SchemaDrift(table, name, "extra_column", None, target[name].describe)
