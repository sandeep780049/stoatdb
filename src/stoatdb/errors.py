"""Error types raised by stoatdb.

Every error the engine raises publicly derives from :class:`StoatError`, so
callers can catch one type and render a friendly message.
"""

from __future__ import annotations


class StoatError(Exception):
    """Base class for all stoatdb errors."""


class ParseError(StoatError):
    """Raised when SQL text cannot be tokenized or parsed."""

    def __init__(self, message: str, pos: int, text: str) -> None:
        self.pos = pos
        self.text = text
        super().__init__(f"{message} at position {pos}: ...{text[pos:pos + 24]!r}")


class ExecutionError(StoatError):
    """Raised for semantic problems: unknown tables/columns, bad types, etc."""


class RowTooLarge(ExecutionError):
    """Raised when a serialized row does not fit inside a single B-tree leaf
    slot. Rows must be under ~2 KiB after encoding; wide blobs should live
    outside the database."""
