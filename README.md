# db-migration-reconciler

The reconciliation step of a database migration that runs with no maintenance
window: prove that the target matches the live source, table by table, and
repair what does not, without ever deleting a row.

This is a reference implementation built as a portfolio sample. It is small on
purpose, so that every decision can be read in an afternoon, and it has not
been used in production. See [About](#about).

## Why this exists

When a migration (say MySQL to PostgreSQL) cannot take the source offline, the
copy is always slightly behind: rows change under the copier, a batch fails
halfway, a type mapping rounds a value, an application dual-writes to one side
only. Before traffic cuts over, someone has to answer "is the target really the
same?" and fix what is not. That check has to be cheap enough to run many
times, resumable when it is interrupted, and gentle enough not to hurt the
system that is still serving customers. This tool is that check.

It is a CLI over SQLAlchemy Core, so it works with any SQLAlchemy URL. SQLite
is the zero-infrastructure demo; PostgreSQL and MySQL drivers are optional
extras.

## What it demonstrates

- **Keyset chunking.** Every read is `WHERE key > :lo ORDER BY key LIMIT n`.
  No `OFFSET`, so the cost of a chunk does not grow with its position in the
  table, and a row inserted or deleted mid-run cannot shift the pages.
- **Order-independent chunk digests.** Each row is normalised to a canonical
  form and hashed; a chunk's digest is the sum of its row hashes modulo 2**256.
  Row order, physical layout and driver typing do not matter; one changed byte
  does.
- **Drill-down diffs.** Only mismatched chunks are compared row by row, and
  the report names the keys and the columns that differ, bounded by
  `--max-diffs`.
- **Dry-run-by-default repair.** `backfill` prints exactly what it would
  write; `--write` applies it through a dialect-specific upsert.
- **Resumable checkpoints.** A JSON file written atomically after every
  committed chunk; an interrupted run resumes after the last reconciled key
  and re-running a finished backfill changes nothing.
- **Dialect isolation.** Type families, upsert statements and SQL rendering
  live in one module; nothing else knows which engine it is talking to.
- **Live-source friendliness.** Short transactions, one chunk per query,
  `--sleep-ms` between chunks, a `--where` grammar that compiles to bound
  parameters, and a refusal to repair by deleting.

## The flow

```
 source (live)                                                target
   │                                                            │
   ├─ 1. schema    tables, columns, type family, nullability ───┤  structural drift: fix DDL first
   ├─ 2. counts    SELECT count(*) per table ───────────────────┤  cheapest signal; a missing and an extra row cancel out
   ├─ 3. checksum  keyset chunks (lo, hi] → one digest per side ┤  mismatched chunk → differing rows → differing columns
   ├─ 4. backfill  dry run → review → --write (upsert) ─────────┤  missing and changed rows repaired, extras reported
   └─ 5. checksum  again ───────────────────────────────────────┘  exit 0: done; exit 1: decide what to do with extras
```

## Quick start

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
reconciler demo --dir ./demo
```

The demo builds `customers`, `orders` and `order_lines` (5,000 seeded rows),
copies them to a target database and then makes the target drift. Everything
below is the real output of the commands shown.

```
$ reconciler demo --dir ./demo
created demo/source.db and demo/target.db
  customers      500 rows
  orders        1500 rows
  order_lines   3000 rows
injected into the target:
  - orders: deleted rows 17, 418 and 1203 in target (missing)
  - customers: rewrote email of rows 42 and 311 in target (changed)
  - order_lines: bumped quantity of row 2500 in target (changed)
  - order_lines: inserted row 3001 only in target (extra)
  - customers.is_active: BOOLEAN in source, INTEGER in target (type family drift)
next: reconciler schema demo/source.db demo/target.db
```

Structure first. A bare file path is accepted as shorthand for an SQLite URL.

```
$ reconciler schema demo/source.db demo/target.db
Schema drift
┏━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━┓
┃ Table     ┃ Column    ┃ Drift ┃ Source         ┃ Target            ┃
┡━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━┩
│ customers │ is_active │ type  │ BOOLEAN [bool] │ INTEGER [integer] │
└───────────┴───────────┴───────┴────────────────┴───────────────────┘
1 drift(s) across 3 table(s)
(exit 1)
```

Counts are the cheapest signal, and also the weakest: `customers` has two
rewritten rows and still counts as equal.

```
$ reconciler counts demo/source.db demo/target.db
Row counts
┏━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━┓
┃ Table       ┃ Source ┃ Target ┃ Difference ┃ Status   ┃
┡━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━┩
│ customers   │ 500    │ 500    │ +0         │ ok       │
│ order_lines │ 3000   │ 3001   │ +1         │ MISMATCH │
│ orders      │ 1500   │ 1497   │ -3         │ MISMATCH │
└─────────────┴────────┴────────┴────────────┴──────────┘
DRIFT: 2 of 3 table(s) differ
(exit 1)
```

The checksum compares content. Only the chunk holding row 2500 mismatches, and
the drill-down names the row, the column and both values. Row 3001 sits past
the source's last key, so it is reported as an extra without a chunk.

```
$ reconciler checksum demo/source.db demo/target.db --table order_lines --chunk 1000
order_lines key=id chunk=1000 columns=5
Mismatched chunks
┏━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Chunk ┃ Range        ┃ Source ┃ Target ┃ Digests                      ┃
┡━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ 2     │ (2000, 3000] │ 1000   │ 1000   │ f15d5e8da66a vs df0c7636d5be │
└───────┴──────────────┴────────┴────────┴──────────────────────────────┘
Differing rows
┏━━━━━━┳━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━┓
┃ Key  ┃ Kind    ┃ Column   ┃ Source ┃ Target ┃
┡━━━━━━╇━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━┩
│ 2500 │ changed │ quantity │ 3      │ 4      │
│ 3001 │ extra   │ -        │ -      │ -      │
└──────┴─────────┴──────────┴────────┴────────┘
DRIFT: 3 chunk(s), 1 mismatched; source rows 3000, target rows 3001; missing 0, changed 1, extra 1
(exit 1)
```

`backfill` is a dry run unless told otherwise. It prints the same list the
write would act on, with the same exit code, so the review is of a concrete
list rather than a plan.

```
$ reconciler backfill demo/source.db demo/target.db --table orders
orders key=id chunk=500 columns=6  mode=DRY RUN
Rows that would change
┏━━━━━━┳━━━━━━━━━┳━━━━━━━━┳━━━━━━━━┳━━━━━━━━┓
┃ Key  ┃ Kind    ┃ Column ┃ Source ┃ Target ┃
┡━━━━━━╇━━━━━━━━━╇━━━━━━━━╇━━━━━━━━╇━━━━━━━━┩
│ 17   │ missing │ -      │ -      │ -      │
│ 418  │ missing │ -      │ -      │ -      │
│ 1203 │ missing │ -      │ -      │ -      │
└──────┴─────────┴────────┴────────┴────────┘
DRIFT: scanned 1500 row(s) in 3 chunk(s); missing 3, changed 0, would write 3; extra 0 (left in place, never deleted)
(exit 1)

$ reconciler backfill demo/source.db demo/target.db --table orders --write --checkpoint ./checkpoint.json
orders key=id chunk=500 columns=6  mode=WRITE
Rows reconciled
┏━━━━━━┳━━━━━━━━━┳━━━━━━━━┳━━━━━━━━┳━━━━━━━━┓
┃ Key  ┃ Kind    ┃ Column ┃ Source ┃ Target ┃
┡━━━━━━╇━━━━━━━━━╇━━━━━━━━╇━━━━━━━━╇━━━━━━━━┩
│ 17   │ missing │ -      │ -      │ -      │
│ 418  │ missing │ -      │ -      │ -      │
│ 1203 │ missing │ -      │ -      │ -      │
└──────┴─────────┴────────┴────────┴────────┘
OK: scanned 1500 row(s) in 3 chunk(s); missing 3, changed 0, wrote 3; extra 0 (left in place, never deleted)
(exit 0)

$ reconciler checksum demo/source.db demo/target.db --table orders
orders key=id chunk=1000 columns=6
OK: 2 chunk(s), 0 mismatched; source rows 1500, target rows 1500; missing 0, changed 0, extra 0
(exit 0)
```

The checkpoint file was removed when the write completed, so the next run
starts from the beginning and proves there is nothing left to do. Running the
same `--write` again scans every chunk and writes nothing.

## Command reference

| Command | What it does | Exit 1 when |
| --- | --- | --- |
| `reconciler demo --dir DIR [--seed N]` | Creates `source.db` and `target.db` with seeded data and injected drift | never |
| `reconciler schema SRC TGT [-t TABLE]...` | Compares tables and columns: name, portable type family, nullability | any drift |
| `reconciler counts SRC TGT [-t TABLE]... [--where P]` | Row count per table on both sides, with the signed difference | any count differs |
| `reconciler checksum SRC TGT -t TABLE [-k KEY] [--chunk N]` | Keyset chunk digests, drill-down diffs for mismatched chunks, extras past the source's last key | any missing, changed or extra row |
| `reconciler backfill SRC TGT -t TABLE [-k KEY] [--chunk N] [--write] [--checkpoint FILE]` | Copies missing rows and overwrites changed ones; dry run unless `--write` | dry run: anything to write or any extra; write: extras remain |

`SRC` and `TGT` are SQLAlchemy URLs (`postgresql+psycopg://...`,
`mysql+pymysql://...`, `sqlite:///file.db`) or bare SQLite file paths.
Passwords are redacted in logs. The key defaults to `id`, must be a single
integer or text column, and is validated on both sides before any read.

Options shared by `checksum` and `backfill`:

| Option | Meaning |
| --- | --- |
| `--columns a,b,c` | Compare (and, for backfill, write) only these columns; the key is always included. For a backfill the list must cover every `NOT NULL` column of the target, or inserts fail. |
| `--where PRED` | Restrict both sides to rows matching `col op literal [AND ...]` (see below) |
| `--max-diffs N` | Keep at most N row-level diffs in the report; counts stay exact and `truncated` is set |
| `--sleep-ms N` | Pause between chunks so a live source only ever sees one short query at a time |
| `--json FILE` | Also write the full report (chunks, digests, diffs) as JSON |
| `-v`, `--verbose` | Log every chunk and the rendered SQL to stderr |

Exit codes: `0` no drift, `1` drift found, `2` usage or configuration error
(unknown table or column, unparseable predicate, missing driver, a checkpoint
that belongs to another table).

### The `--where` grammar

```
predicate  := comparison ("AND" comparison)*
comparison := column op literal | column "IS" ["NOT"] "NULL"
op         := "=" | "!=" | "<>" | "<" | "<=" | ">" | ">="
literal    := number | 'string' | TRUE | FALSE
```

Column names are checked against the reflected table and literals become bound
parameters, so the predicate cannot inject SQL even when another tool builds
the command line. `OR`, functions and subqueries are rejected with exit code 2
on purpose: anything richer belongs in a view on the source database, where it
can be reviewed like any other query.

## Design decisions

Each of these has a short ADR under [`docs/adr/`](docs/adr/).

- **Keyset pagination, never OFFSET** ([ADR 0001](docs/adr/0001-keyset-chunking.md)).
  `OFFSET` re-walks the skipped rows on every page and skips or repeats rows
  when the table changes underneath it. `WHERE key > :lo ORDER BY key LIMIT n`
  is an index range scan at any position. Trade-off: the key must be a single
  orderable column, and chunks are not a consistent snapshot; the operational
  answer to a row changing mid-run is to run the checksum twice, not to hold
  a long transaction on a live source.
- **The source defines the chunk boundaries.** Each source chunk's key range
  `(lo, hi]` is read from the target as a closed range rather than as "the next
  n rows", so a row missing on one side changes one chunk instead of shifting
  every later boundary and flagging the whole table. Rows past the source's
  last key are read separately and can only be extras.
- **Order-independent digests** ([ADR 0002](docs/adr/0002-order-independent-chunk-digests.md)).
  Sum of per-row SHA-256 hashes modulo 2**256 over a canonical, normalised row
  encoding. Commutative, incremental, and sensitive to any single change. Sum
  rather than XOR because XOR of two identical rows cancels out, which a target
  without a unique constraint could produce. Trade-off: values that differ only
  in representation (`1`/`TRUE`, `10.50`/`10.5`, naive UTC vs `+00:00`) compare
  equal by design, which is what a cross-engine migration needs; the schema
  check is where intentional type changes get reviewed.
- **Never delete** ([ADR 0003](docs/adr/0003-dry-run-and-never-delete.md)).
  A target row the source lacks might be the only copy of something an
  application dual-wrote, or a row the source just deleted. The tool reports
  extras and exits 1; removing them is a human decision taken with a query the
  human wrote. Trade-off: a fully repaired table with extras still fails the
  run, deliberately.
- **Dry run by default.** `backfill` writes only with `--write`, and the dry
  run produces the exact list and exit code the write would. Trade-off: one
  more invocation, in exchange for a destructive path that is always explicit.
- **Checkpoint file, not a table in the target.** The target schema belongs to
  the product being migrated; it should not gain tool tables. A JSON file next
  to the operator is inspectable, disposable and written atomically
  (write-then-rename) after each committed chunk. The upsert is idempotent, so
  a crash between the commit and the checkpoint costs one repeated chunk.
  Trade-off: a checkpoint is local to the machine that ran the backfill.
- **A grammar for `--where` instead of raw SQL.** Raw predicates would make the
  CLI an injection vector the moment it is wrapped by automation. The grammar
  covers the realistic cases (a tenant, a date range, a status) and refuses the
  rest loudly.
- **Dialect isolation.** `dialects.py` owns the type-family mapping, the
  upsert statement (`ON CONFLICT` for SQLite and PostgreSQL, `ON DUPLICATE KEY`
  for MySQL and MariaDB) and SQL rendering; everything else is SQLAlchemy Core,
  which also means identifier quoting is never hand-written. The SQLite branch
  is tested end to end; the PostgreSQL and MySQL statements are tested at the
  compiled-SQL level.

## Running against PostgreSQL or MySQL

```sh
pip install ".[postgres]"   # psycopg 3
pip install ".[mysql]"      # PyMySQL
reconciler counts   "mysql+pymysql://app:***@legacy-db/shop" "postgresql+psycopg://app:***@new-db/shop"
reconciler checksum "mysql+pymysql://app:***@legacy-db/shop" "postgresql+psycopg://app:***@new-db/shop" \
    --table orders --chunk 5000 --sleep-ms 50 --json orders.json
```

Practical notes for a live source: run from a host close to both databases,
size `--chunk` so that a single chunk query finishes in well under a second,
use `--sleep-ms` to keep the load in the background, and treat drift that
disappears on a second run as in-flight writes rather than a bug. A schema
drift such as `TINYINT(1)` to `BOOLEAN` is reported every run; that is the
intended review list for a cross-engine migration, not something to silence.

## Deliberately out of scope

- **Change data capture or streaming replication.** This is the verification
  and repair step that runs next to a replication mechanism, not a replacement
  for one.
- **Composite keys.** Keyset pagination over several columns needs tuple
  comparison, which not every engine supports the same way. Add a surrogate
  key or reconcile through a view that exposes one.
- **Large-object columns.** Hashing a multi-gigabyte blob per row is a
  different problem; compare such columns through a stored length or digest
  column and exclude the blob with `--columns`.
- **An allow-list for intentional type changes.** The schema check reports
  every family difference; deciding which are intended is a review step.
- **Schema repair, parallelism and consistent snapshots.** DDL is generated
  elsewhere; run one process per table for parallelism; and see ADR 0001 for
  why a long snapshot transaction is the wrong tool on a live source.

## Development

```sh
pip install -e ".[dev]"
ruff check . && ruff format --check . && mypy --strict src && pytest
```

The test suite builds its own SQLite databases per test, treats every warning
as an error, and covers schema drift, counts, chunk boundaries, order
independence, drill-down diffs, dry run, repair, idempotent re-runs and resume
from a checkpoint after a simulated crash. CI runs the same commands on Python
3.12 and 3.13 and builds the Docker image.

```sh
docker build -t reconciler .
docker run --rm -v "$PWD:/work" reconciler demo --dir ./demo
docker run --rm -v "$PWD:/work" reconciler checksum demo/source.db demo/target.db --table orders
```

Source layout: `src/reconciler/` holds one module per concern (`cli`,
`adapters`, `dialects`, `predicate`, `schema`, `counts`, `checksum`,
`backfill`, `checkpoint`, `report`, `demo`), about 1,500 lines in total.

## About

Built by [Hardyal Singh Deora](https://hardyalsinghdeora.pages.dev)
([GitHub](https://github.com/hardyalsinghdeora)) as a portfolio sample: a
reference implementation of the tooling a careful engineer writes for a
migration that has to run with no maintenance window. It is not extracted from
any employer's codebase and has not been used in production.

Licensed under the [MIT License](LICENSE).
