# ADR 0003: Dry run by default, and never delete target rows

## Status

Accepted.

## Context

The backfill is the only command that writes, and it writes to the system that
is about to become the system of record. The two ways to lose data here are
writing when nobody meant to, and deleting a row that only looks like it should
not exist: on a live source, a row can be missing because it was just deleted,
but on the target it may be the only copy of something an application wrote
there during a dual-write phase, or a row inserted by a migration step that the
source simply does not have.

## Decision

`reconciler backfill` is a dry run unless `--write` is given. The dry run runs
the full comparison and prints exactly what the write would do, with the same
exit code, so the operator reviews a concrete list, not a plan.

The write only inserts missing rows and overwrites changed ones, through an
upsert keyed on the reconciliation key. Target rows the source does not have are
counted and listed as `extra`, and they make the run exit with code 1, but they
are never deleted. Deleting is a human decision, taken with a query the human
wrote.

Progress is recorded in a checkpoint file rather than a table in the target.
The target schema is someone else's (it is the migrated product), it must not
gain tool-specific tables, and a file next to the operator is trivially
inspectable and disposable. The file is written atomically after each committed
chunk, so a crash costs at most one repeated chunk, and the upsert makes that
repeat harmless.

## Consequences

- A clean `--write` run on a table with extras still exits 1, which is the
  signal to look at the extras rather than to re-run.
- Repairing requires a second invocation. That is the cost of making the
  destructive path explicit; the dry run output is designed to be pasted into
  a change ticket.
- The checkpoint stores only the table, key, last key and counters. It is
  refused for a different table or key, and removed when the run completes so
  the next run starts from the beginning and proves there is nothing left.
