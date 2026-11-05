"""Shared fixtures: small SQLite pairs built per test, disposed afterwards."""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from reconciler.adapters import Database, open_database

ROW_COUNT = 25


def item_rows(count: int = ROW_COUNT) -> list[dict[str, Any]]:
    """Deterministic rows with one column per type family that matters."""
    return [
        {
            "id": i,
            "name": f"item-{i}",
            "qty": i * 3,
            "price": Decimal(i) / 4,
            "active": i % 2 == 0,
            "created": dt.datetime(2024, 1, 1) + dt.timedelta(hours=i),
            "note": None if i % 5 == 0 else f"note {i}",
        }
        for i in range(1, count + 1)
    ]


def item_metadata(
    drop: str | None = None,
    nullable: tuple[str, ...] = ("note",),
    **overrides: sa.types.TypeEngine[Any],
) -> sa.MetaData:
    """The ``items`` table; ``drop`` removes a column, ``nullable`` lists the optional ones."""
    columns: dict[str, sa.types.TypeEngine[Any]] = {
        "name": sa.String(40),
        "qty": sa.Integer(),
        "price": sa.Numeric(10, 2),
        "active": sa.Boolean(),
        "created": sa.DateTime(),
        "note": sa.Text(),
    }
    columns.update(overrides)
    if drop:
        columns.pop(drop)
    metadata = sa.MetaData()
    sa.Table(
        "items",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        *(sa.Column(name, kind, nullable=name in nullable) for name, kind in columns.items()),
    )
    return metadata


def build_items_db(
    path: Path, rows: list[dict[str, Any]], metadata: sa.MetaData | None = None
) -> str:
    """Create an SQLite file holding ``items`` and return its path (usable as a URL)."""
    metadata = metadata or item_metadata()
    engine = sa.create_engine(f"sqlite:///{path}")
    metadata.create_all(engine)
    if rows:
        with engine.begin() as connection:
            connection.execute(sa.insert(metadata.tables["items"]), rows)
    engine.dispose()
    return str(path)


def read_items(path: str) -> dict[int, dict[str, Any]]:
    engine = sa.create_engine(f"sqlite:///{path}")
    metadata = sa.MetaData()
    metadata.reflect(bind=engine)
    with engine.connect() as connection:
        rows = connection.execute(sa.select(metadata.tables["items"])).mappings().all()
    engine.dispose()
    return {int(row["id"]): dict(row) for row in rows}


class DatabasePair:
    """Opens the source and target on demand and disposes whatever was opened."""

    def __init__(self, source: str, target: str) -> None:
        self.source_path, self.target_path = source, target
        self._opened: list[Database] = []

    def open(self, tables: list[str] | None = None) -> tuple[Database, Database]:
        source, target = (
            open_database(self.source_path, tables),
            open_database(self.target_path, tables),
        )
        self._opened += [source, target]
        return source, target

    def close(self) -> None:
        for database in self._opened:
            database.dispose()
        self._opened.clear()


@pytest.fixture(autouse=True)
def _reset_package_logging() -> Iterator[None]:
    yield
    logging.getLogger("reconciler").handlers.clear()


@pytest.fixture
def pair(tmp_path: Path) -> Iterator[DatabasePair]:
    """Identical source and target, 25 rows each."""
    rows = item_rows()
    databases = DatabasePair(
        build_items_db(tmp_path / "source.db", rows),
        build_items_db(tmp_path / "target.db", rows),
    )
    yield databases
    databases.close()
