"""SQL tokenizer.

Turns raw SQL text into a stream of tokens with source positions. Comments
(``--`` to end of line) are skipped, keywords are recognised
case-insensitively, and identifiers may be quoted with double quotes.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import ParseError

KEYWORDS: frozenset[str] = frozenset(
    {
        "SELECT", "FROM", "WHERE", "AND", "OR", "NOT", "INSERT", "INTO",
        "VALUES", "CREATE", "TABLE", "UPDATE", "SET", "DELETE", "ORDER",
        "BY", "ASC", "DESC", "LIMIT", "OFFSET", "JOIN", "ON", "GROUP",
        "AS", "DISTINCT", "IS", "NULL", "LIKE", "IN", "PRIMARY", "KEY",
        "INTEGER", "REAL", "TEXT", "DROP", "IF", "EXISTS",
    }
)

AGGREGATES: frozenset[str] = frozenset({"COUNT", "SUM", "AVG", "MIN", "MAX"})

TWO_CHAR_OPS: tuple[str, ...] = ("<=", ">=", "<>", "!=")
ONE_CHAR_OPS: str = "=<>(),.*+-/;"


@dataclass(frozen=True)
class Token:
    kind: str  # "KW", "IDENT", "NUMBER", "STRING", "OP", "EOF"
    value: str
    pos: int

    def is_kw(self, *names: str) -> bool:
        return self.kind == "KW" and self.value.upper() in names


def tokenize(text: str) -> list[Token]:
    """Convert *text* into a token list ending with an EOF token."""
    tokens: list[Token] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in " \t\r\n":
            i += 1
            continue
        if text.startswith("--", i):
            end = text.find("\n", i)
            i = n if end == -1 else end + 1
            continue
        start = i
        if ch.isdigit():
            i = _consume_number(text, i)
            tokens.append(Token("NUMBER", text[start:i], start))
            continue
        if ch == "'":
            value, i = _consume_string(text, i)
            tokens.append(Token("STRING", value, start))
            continue
        if ch == '"' or ch == "`":
            end = text.find(ch, i + 1)
            if end == -1:
                raise ParseError("unterminated quoted identifier", i, text)
            tokens.append(Token("IDENT", text[i + 1:end], start))
            i = end + 1
            continue
        if ch.isalpha() or ch == "_":
            while i < n and (text[i].isalnum() or text[i] == "_"):
                i += 1
            word = text[start:i]
            kind = "KW" if word.upper() in KEYWORDS else "IDENT"
            tokens.append(Token(kind, word, start))
            continue
        two = text[i:i + 2]
        if two in TWO_CHAR_OPS:
            tokens.append(Token("OP", "<>" if two == "!=" else two, start))
            i += 2
            continue
        if ch in ONE_CHAR_OPS:
            tokens.append(Token("OP", ch, start))
            i += 1
            continue
        raise ParseError(f"unexpected character {ch!r}", i, text)
    tokens.append(Token("EOF", "", n))
    return tokens


def split_statements(sql: str) -> list[str]:
    """Split *sql* into individual statements at top-level ``;`` separators.

    Strings, quoted identifiers and comments are respected because the text is
    cut on token boundaries rather than on raw characters. Whitespace-only
    statements are dropped.
    """
    tokens = tokenize(sql)
    statements: list[str] = []
    start: int | None = None
    for tok in tokens:
        if tok.kind == "EOF":
            break
        if start is None:
            start = tok.pos
        if tok.kind == "OP" and tok.value == ";":
            statements.append(sql[start:tok.pos])
            start = None
    if start is not None:
        statements.append(sql[start:tokens[-1].pos])
    return [s for s in statements if s.strip()]


def _consume_number(text: str, i: int) -> int:
    n = len(text)
    while i < n and text[i].isdigit():
        i += 1
    if i < n - 1 and text[i] == "." and text[i + 1].isdigit():
        i += 1
        while i < n and text[i].isdigit():
            i += 1
    return i


def _consume_string(text: str, i: int) -> tuple[str, int]:
    """Consume a single-quoted string starting at *i*, returning the decoded
    value and the index just past the closing quote. ``''`` escapes a quote."""
    n = len(text)
    i += 1
    parts: list[str] = []
    chunk_start = i
    while i < n:
        if text[i] == "'":
            if i + 1 < n and text[i + 1] == "'":
                parts.append(text[chunk_start:i])
                parts.append("'")
                i += 2
                chunk_start = i
                continue
            parts.append(text[chunk_start:i])
            return "".join(parts), i + 1
        i += 1
    raise ParseError("unterminated string literal", i - 1, text)
