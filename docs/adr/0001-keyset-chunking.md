# ADR 0001: Walk tables with keyset chunks, never OFFSET

## Status

Accepted.

## Context

Both the checksum and the backfill have to read every row of a table that may
hold hundreds of millions of rows, on a source that is serving production
traffic at the same time. The obvious `LIMIT n OFFSET m` loop gets slower with
every page, because the engine still has to walk and discard the first `m`
rows, and it silently skips or repeats rows when the table changes between
pages. Both problems get worse exactly when the table is large and live.

## Decision

Every read is `WHERE key > :lo ORDER BY key LIMIT n` on a unique, orderable key
(the primary key by default). The last key of each chunk becomes the lower
bound of the next, so each query is an index range scan whose cost does not
depend on how far into the table it is. The source defines the chunk
boundaries; the target is then read for the same closed range `(lo, hi]`, so a
row missing on one side changes one chunk's digest instead of shifting the
boundaries of every later chunk.

Each chunk is read in its own short transaction, and `--sleep-ms` adds a pause
between chunks so a long reconciliation stays a background load.

## Consequences

- Reads stay cheap at any table size and any position in the table.
- The key must be a single integer or text column. Composite keys are out of
  scope (see the README); they would need tuple comparison, which not every
  engine supports.
- Chunks are not a consistent snapshot of the table. A row modified between the
  source and target reads of its chunk shows up as drift; running the checksum
  twice and comparing is the operational answer, not a longer transaction.
- The last key of each chunk is also what the backfill checkpoint records, so
  resuming is the same query with a different `:lo`.
