"""Everything that differs between database engines lives here.

The rest of the package speaks SQLAlchemy Core, so identifier quoting is
delegated to the engine's own preparer and never written by hand. Adding an
engine means adding a branch in this module and nowhere else.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Any

from sqlalchemy import Dialect, Table
from sqlalchemy import types as sqltypes
from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.sql import ClauseElement
from sqlalchemy.sql.expression import Insert

from reconciler import ReconcilerError


class TypeFamily(StrEnum):
    """Portable classes of column types; engines differ only below this level."""

    INTEGER = "integer"
    TEXT = "text"
    DECIMAL = "decimal"
    DATETIME = "datetime"
    BOOL = "bool"
    BINARY = "binary"
    OTHER = "other"


# Order matters: Boolean goes before Integer because some engines model it as
# a small integer, and Float is listed next to Numeric because SQLAlchemy 2.1
# stopped making one a subclass of the other.
_FAMILIES: tuple[tuple[type[sqltypes.TypeEngine[Any]], TypeFamily], ...] = (
    (sqltypes.Boolean, TypeFamily.BOOL),
    (sqltypes.Integer, TypeFamily.INTEGER),
    (sqltypes.Numeric, TypeFamily.DECIMAL),
    (sqltypes.Float, TypeFamily.DECIMAL),
    (sqltypes.String, TypeFamily.TEXT),
    (sqltypes.DateTime, TypeFamily.DATETIME),
    (sqltypes.Date, TypeFamily.DATETIME),
    (sqltypes.Time, TypeFamily.DATETIME),
    (sqltypes.LargeBinary, TypeFamily.BINARY),
    (sqltypes.BINARY, TypeFamily.BINARY),
    (sqltypes.VARBINARY, TypeFamily.BINARY),
)


def type_family(column_type: sqltypes.TypeEngine[Any]) -> TypeFamily:
    """Map a reflected column type onto its portable family."""
    for base, family in _FAMILIES:
        if isinstance(column_type, base):
            return family
    return TypeFamily.OTHER


def render_sql(clause: ClauseElement, dialect: Dialect) -> str:
    """Render a Core clause as text in the engine's quoting style, for reports and logs only."""
    return str(clause.compile(dialect=dialect, compile_kwargs={"literal_binds": True}))


def upsert(dialect: Dialect, table: Table, key: str, columns: Sequence[str]) -> Insert:
    """Build an INSERT that overwrites the existing row when ``key`` already exists.

    The statement carries no values: execute it with a list of row dicts so the
    driver can batch it (executemany).
    """
    updates = [column for column in columns if column != key]
    if dialect.name == "sqlite":
        sqlite_stmt = sqlite.insert(table)
        if not updates:
            return sqlite_stmt.on_conflict_do_nothing(index_elements=[key])
        return sqlite_stmt.on_conflict_do_update(
            index_elements=[key], set_={c: sqlite_stmt.excluded[c] for c in updates}
        )
    if dialect.name == "postgresql":
        pg_stmt = postgresql.insert(table)
        if not updates:
            return pg_stmt.on_conflict_do_nothing(index_elements=[key])
        return pg_stmt.on_conflict_do_update(
            index_elements=[key], set_={c: pg_stmt.excluded[c] for c in updates}
        )
    if dialect.name in {"mysql", "mariadb"}:
        my_stmt = mysql.insert(table)
        # MySQL has no DO NOTHING; re-assigning the key is the idiomatic no-op.
        return my_stmt.on_duplicate_key_update({c: my_stmt.inserted[c] for c in (updates or [key])})
    raise ReconcilerError(f"backfill is not implemented for dialect {dialect.name!r}")
