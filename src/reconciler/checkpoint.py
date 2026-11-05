"""Resume state for backfill runs: a small JSON file, written atomically after each chunk."""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from reconciler import ReconcilerError

Key = int | str


@dataclass(frozen=True)
class Checkpoint:
    table: str
    key: str
    last_key: Key  # every source row with key <= last_key has been reconciled
    chunks_done: int
    rows_written: int
    updated_at: str

    @classmethod
    def now(cls, table: str, key: str, last_key: Key, chunks: int, written: int) -> Checkpoint:
        stamp = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        return cls(table, key, last_key, chunks, written, stamp)


def load(path: Path, table: str, key: str) -> Checkpoint | None:
    """Return the checkpoint if one exists for exactly this table and key."""
    if not path.exists():
        return None
    try:
        checkpoint = Checkpoint(**json.loads(path.read_text()))
    except (TypeError, ValueError) as exc:
        raise ReconcilerError(f"checkpoint {path} is not readable: {exc}") from exc
    if (checkpoint.table, checkpoint.key) != (table, key):
        raise ReconcilerError(
            f"checkpoint {path} belongs to table {checkpoint.table!r} key {checkpoint.key!r}; "
            "pass a different --checkpoint path"
        )
    if not isinstance(checkpoint.last_key, int | str):
        raise ReconcilerError(f"checkpoint {path} has an unusable last_key")
    return checkpoint


def save(path: Path, checkpoint: Checkpoint) -> None:
    # Write-then-rename so a crash mid-write never leaves a half-written file
    # that the next run would refuse to parse.
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(asdict(checkpoint), indent=2))
    temporary.replace(path)


def clear(path: Path) -> None:
    path.unlink(missing_ok=True)
