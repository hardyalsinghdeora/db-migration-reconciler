"""A deliberately tiny ``--where`` grammar that compiles to bound parameters.

Accepting raw SQL would make the CLI an injection vector the moment another
tool wraps it, so only ``column op literal`` comparisons joined by AND are
allowed. Anything richer belongs in a view on the source database.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from sqlalchemy import ColumnElement, Table, and_

from reconciler import ReconcilerError

_TOKEN = re.compile(
    r"\s*(?:(?P<op><=|>=|<>|!=|=|<|>)"
    r"|(?P<num>-?\d+(?:\.\d+)?)"
    r"|'(?P<str>(?:[^']|'')*)'"
    r"|(?P<word>[A-Za-z_][A-Za-z0-9_]*))"
)
_OPERATORS: dict[str, Callable[[ColumnElement[Any], object], ColumnElement[bool]]] = {
    "=": lambda column, value: column == value,
    "!=": lambda column, value: column != value,
    "<>": lambda column, value: column != value,
    "<": lambda column, value: column < value,
    "<=": lambda column, value: column <= value,
    ">": lambda column, value: column > value,
    ">=": lambda column, value: column >= value,
}
_WORDS: dict[str, object] = {"TRUE": True, "FALSE": False}

Token = tuple[str, str]


def parse_where(text: str, table: Table) -> ColumnElement[bool]:
    """Compile ``col op literal [AND ...]`` against ``table``'s columns."""
    tokens = _tokenise(text.strip())
    clauses: list[ColumnElement[bool]] = []
    position = 0
    while True:
        clause, position = _comparison(tokens, position, table)
        clauses.append(clause)
        if position == len(tokens):
            return and_(*clauses)
        if _word(tokens, position) != "AND":
            raise ReconcilerError(f"--where: expected AND near {tokens[position][1]!r}")
        position += 1


def _tokenise(text: str) -> list[Token]:
    tokens: list[Token] = []
    position = 0
    while position < len(text):
        match = _TOKEN.match(text, position)
        if match is None or match.lastgroup is None:
            raise ReconcilerError(f"--where: cannot parse near {text[position:]!r}")
        tokens.append((match.lastgroup, match.group(match.lastgroup)))
        position = match.end()
    if not tokens:
        raise ReconcilerError("--where: predicate is empty")
    return tokens


def _word(tokens: list[Token], position: int) -> str | None:
    if position < len(tokens) and tokens[position][0] == "word":
        return tokens[position][1].upper()
    return None


def _comparison(
    tokens: list[Token], position: int, table: Table
) -> tuple[ColumnElement[bool], int]:
    kind, name = tokens[position] if position < len(tokens) else ("", "")
    if kind != "word" or name not in table.c:
        raise ReconcilerError(f"--where: unknown column {name!r} in table {table.name!r}")
    column = table.c[name]
    if _word(tokens, position + 1) == "IS":
        if _word(tokens, position + 2) == "NULL":
            return column.is_(None), position + 3
        if _word(tokens, position + 2) == "NOT" and _word(tokens, position + 3) == "NULL":
            return column.is_not(None), position + 4
        raise ReconcilerError(f"--where: expected NULL or NOT NULL after {name} IS")
    if position + 2 >= len(tokens) or tokens[position + 1][0] != "op":
        raise ReconcilerError(f"--where: expected an operator and a value after {name!r}")
    operator = _OPERATORS[tokens[position + 1][1]]
    return operator(column, _literal(tokens[position + 2])), position + 3


def _literal(token: Token) -> object:
    kind, text = token
    if kind == "num":
        return int(text) if text.lstrip("-").isdigit() else float(text)
    if kind == "str":
        return text.replace("''", "'")
    if kind == "word" and text.upper() in _WORDS:
        return _WORDS[text.upper()]
    raise ReconcilerError(f"--where: expected a number, 'string', TRUE or FALSE, got {text!r}")
