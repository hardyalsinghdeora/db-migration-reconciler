"""A self-contained SQLite pair with seeded data and injected drift, for trying the CLI."""

from __future__ import annotations

import datetime as dt
import random
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import sqlalchemy as sa

ROWS = {"customers": 500, "orders": 1500, "order_lines": 3000}
_FIRST = "Aarav Ana Chen Dmitri Elena Fatima Grace Hiro Ines Jonas Kavya Luca Maya Noah"
_LAST = "Ahmed Baker Costa Evans Fischer Garcia Hughes Ito Jensen Khan Lopez Moreau Nair"
_COUNTRIES = ["IN", "DE", "US", "GB", "BR", "JP", "NG", "FR"]
_STATUSES = ["pending", "paid", "shipped", "cancelled"]


@dataclass(frozen=True)
class DemoResult:
    source: Path
    target: Path
    rows: dict[str, int]
    injected: list[str]


def schema(*, bool_as_integer: bool) -> sa.MetaData:
    """Three related tables; the target variant stores booleans as integers."""
    metadata = sa.MetaData()
    sa.Table(
        "customers",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("email", sa.String(120), nullable=False),
        sa.Column("full_name", sa.String(80), nullable=False),
        sa.Column("country", sa.String(2), nullable=False),
        sa.Column("is_active", sa.Integer if bool_as_integer else sa.Boolean, nullable=False),
        sa.Column("credit_limit", sa.Numeric(10, 2)),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )
    sa.Table(
        "orders",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("customer_id", sa.Integer, sa.ForeignKey("customers.id"), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("placed_at", sa.DateTime, nullable=False),
        sa.Column("total_cents", sa.Integer, nullable=False),
        sa.Column("note", sa.Text),
    )
    sa.Table(
        "order_lines",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("order_id", sa.Integer, sa.ForeignKey("orders.id"), nullable=False),
        sa.Column("sku", sa.String(12), nullable=False),
        sa.Column("quantity", sa.Integer, nullable=False),
        sa.Column("unit_price", sa.Numeric(10, 2), nullable=False),
    )
    return metadata


def seed_rows(seed: int) -> dict[str, list[dict[str, Any]]]:
    """Deterministic data: the same seed always produces the same rows."""
    rng = random.Random(seed)
    return {
        "customers": [_customer(rng, i) for i in range(1, ROWS["customers"] + 1)],
        "orders": [_order(rng, i) for i in range(1, ROWS["orders"] + 1)],
        "order_lines": [_order_line(rng, i) for i in range(1, ROWS["order_lines"] + 1)],
    }


def _moment(rng: random.Random) -> dt.datetime:
    return dt.datetime(2024, 1, 1) + dt.timedelta(minutes=rng.randrange(0, 525_600))


def _customer(rng: random.Random, i: int) -> dict[str, Any]:
    first, last = rng.choice(_FIRST.split()), rng.choice(_LAST.split())
    return {
        "id": i,
        "email": f"{first.lower()}.{last.lower()}{i}@example.com",
        "full_name": f"{first} {last}",
        "country": rng.choice(_COUNTRIES),
        "is_active": rng.random() < 0.9,
        "credit_limit": None if rng.random() < 0.2 else Decimal(rng.randrange(500, 20_000)),
        "created_at": _moment(rng),
    }


def _order(rng: random.Random, i: int) -> dict[str, Any]:
    return {
        "id": i,
        "customer_id": rng.randrange(1, ROWS["customers"] + 1),
        "status": rng.choice(_STATUSES),
        "placed_at": _moment(rng),
        "total_cents": rng.randrange(100, 250_000),
        "note": None if rng.random() < 0.7 else f"gift wrap: {rng.choice(['yes', 'no'])}",
    }


def _order_line(rng: random.Random, i: int) -> dict[str, Any]:
    return {
        "id": i,
        "order_id": rng.randrange(1, ROWS["orders"] + 1),
        "sku": f"SKU-{rng.randrange(1000, 9999)}",
        "quantity": rng.randrange(1, 6),
        "unit_price": Decimal(rng.randrange(99, 50_000)) / 100,
    }


def build_demo(directory: Path, seed: int = 42) -> DemoResult:
    """Create source.db and target.db, copy the data, then make the target drift."""
    directory.mkdir(parents=True, exist_ok=True)
    source, target = directory / "source.db", directory / "target.db"
    rows = seed_rows(seed)
    for path, bool_as_integer in ((source, False), (target, True)):
        path.unlink(missing_ok=True)
        engine = sa.create_engine(f"sqlite:///{path}")
        metadata = schema(bool_as_integer=bool_as_integer)
        metadata.create_all(engine)
        with engine.begin() as connection:
            for name, table_rows in rows.items():
                connection.execute(sa.insert(metadata.tables[name]), table_rows)
            if bool_as_integer:
                injected = _inject_drift(connection, metadata)
        engine.dispose()
    return DemoResult(source, target, {name: len(r) for name, r in rows.items()}, injected)


def _inject_drift(connection: sa.Connection, metadata: sa.MetaData) -> list[str]:
    customers, orders, lines = (
        metadata.tables[name] for name in ("customers", "orders", "order_lines")
    )
    connection.execute(sa.delete(orders).where(orders.c.id.in_([17, 418, 1203])))
    connection.execute(
        sa.update(customers)
        .where(customers.c.id.in_([42, 311]))
        .values(email=sa.literal("drift+") + sa.cast(customers.c.id, sa.String) + "@example.com")
    )
    connection.execute(
        sa.update(lines).where(lines.c.id == 2500).values(quantity=lines.c.quantity + 1)
    )
    connection.execute(
        sa.insert(lines),
        {"id": 3001, "order_id": 1, "sku": "SKU-EXTRA", "quantity": 1, "unit_price": Decimal(1)},
    )
    return [
        "orders: deleted rows 17, 418 and 1203 in target (missing)",
        "customers: rewrote email of rows 42 and 311 in target (changed)",
        "order_lines: bumped quantity of row 2500 in target (changed)",
        "order_lines: inserted row 3001 only in target (extra)",
        "customers.is_active: BOOLEAN in source, INTEGER in target (type family drift)",
    ]
