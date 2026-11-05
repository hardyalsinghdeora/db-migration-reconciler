"""Engine construction and schema reflection: the only place URLs are handled."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine, MetaData, Table, create_engine, make_url
from sqlalchemy.exc import SQLAlchemyError

from reconciler import ReconcilerError

log = logging.getLogger(__name__)


def normalise_url(url: str) -> str:
    """Accept a bare file path as shorthand for an SQLite URL."""
    return url if "://" in url else f"sqlite:///{url}"


@dataclass(frozen=True)
class Database:
    """An engine plus the reflected definitions of the tables it exposes."""

    engine: Engine
    metadata: MetaData

    @property
    def label(self) -> str:
        return self.engine.url.render_as_string(hide_password=True)

    def table_names(self) -> list[str]:
        return sorted(self.metadata.tables)

    def table(self, name: str) -> Table:
        try:
            return self.metadata.tables[name]
        except KeyError:
            raise ReconcilerError(f"table {name!r} not found in {self.label}") from None

    def dispose(self) -> None:
        self.engine.dispose()


def open_database(url: str, tables: Sequence[str] | None = None) -> Database:
    """Connect and reflect ``tables`` (default: every table in the default schema)."""
    full_url = normalise_url(url)
    _refuse_missing_sqlite_file(full_url)
    try:
        engine = create_engine(full_url)
    except ModuleNotFoundError as exc:
        raise ReconcilerError(
            f"driver for {full_url.split(':', 1)[0]!r} is not installed ({exc.name}); "
            "install the matching extra, e.g. pip install 'db-migration-reconciler[postgres]'"
        ) from exc
    except SQLAlchemyError as exc:
        raise ReconcilerError(f"cannot open {url}: {exc}") from exc
    metadata = MetaData()
    try:
        metadata.reflect(bind=engine, only=list(tables) if tables else None)
    except SQLAlchemyError as exc:
        engine.dispose()
        raise ReconcilerError(f"cannot open {url}: {exc}") from exc
    log.debug(
        "opened %s tables=%d", engine.url.render_as_string(hide_password=True), len(metadata.tables)
    )
    return Database(engine, metadata)


def _refuse_missing_sqlite_file(url: str) -> None:
    # SQLite silently creates an empty database for an unknown path, which would
    # turn a typo into a clean-looking "target has no rows" report.
    parsed = make_url(url)
    is_file = parsed.drivername.startswith("sqlite") and parsed.database not in (
        None,
        "",
        ":memory:",
    )
    if is_file and not Path(str(parsed.database)).exists():
        raise ReconcilerError(f"SQLite file {parsed.database!r} does not exist")
