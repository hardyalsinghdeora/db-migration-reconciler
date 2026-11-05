# ADR 0002: Order-independent chunk digests

## Status

Accepted.

## Context

To compare a chunk without shipping every row across the network twice, each
side computes a digest and only the digests are compared. The natural choice,
hashing the concatenation of rows, depends on row order. Two engines return
rows in index order for `ORDER BY key`, but the rows inside a chunk may differ
in physical layout, collation and driver typing, and a digest that depends on
any of that reports drift where there is none.

## Decision

Each row is reduced to a canonical form first: the selected columns in a fixed
order, each value normalised (booleans and integral numbers become integers,
decimals lose trailing zeros, timestamps become naive UTC ISO-8601 strings,
bytes become hex) and serialised as compact JSON. The row hash is the SHA-256 of
that string, read as a 256-bit integer. The chunk digest is the sum of its row
hashes modulo 2**256.

Summing is commutative, so the digest does not depend on the order in which
rows arrive. It is also incremental, which keeps the door open for maintaining a
running digest instead of recomputing it. Sum was chosen over XOR because XOR
of two identical rows cancels to zero; keys make rows unique within a table, but
a target without the unique constraint could hold the same row twice and XOR
would not notice.

## Consequences

- A single changed byte in any row changes the chunk digest, and the only way
  to drill down is to compare the chunk's rows, which the tool then does.
- Values that differ only in representation compare equal: `1` and `TRUE`,
  `10.50` and `10.5`, a naive timestamp and the same instant with `+00:00`.
  This is what a cross-engine migration needs, and it means the schema check
  (type families) is the place where intentional type changes are reviewed.
- Strings and numbers never compare equal (`"100"` is not `100`), so a column
  that changed from text to integer is reported as changed on every row, which
  is the honest answer.
- The canonical form is a hashing input only; reports show the original values.
