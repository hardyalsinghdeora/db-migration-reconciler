"""Verify and repair a table-by-table database migration while the source stays live."""

__version__ = "0.1.0"


class ReconcilerError(Exception):
    """A usage or configuration problem the operator has to fix (exit code 2)."""
