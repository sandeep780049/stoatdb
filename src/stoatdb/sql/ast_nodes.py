"""Abstract syntax tree for the stoatdb SQL dialect.

Nodes are plain dataclasses; the parser produces them, the executor consumes
them. Only the subset of SQL documented in the README is represented.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ---------------------------------------------------------------- expressions

@dataclass(frozen=True)
class Literal:
    value: int | float | str | None


@dataclass(frozen=True)
class Column:
    """A column reference: ``col`` or ``table.col``."""
    table: str | None
    name: str


@dataclass(frozen=True)
class Star:
    """``*`` inside COUNT(*) or a select list."""


@dataclass(frozen=True)
class Unary:
    op: str  # "-" or "NOT"
    operand: Expr


@dataclass(frozen=True)
class Binary:
    op: str  # AND OR = <> < <= > >= + - * /
    left: Expr
    right: Expr


@dataclass(frozen=True)
class IsNull:
    operand: Expr
    negated: bool


@dataclass(frozen=True)
class Like:
    operand: Expr
    pattern: Expr
    negated: bool


@dataclass(frozen=True)
class InList:
    operand: Expr
    items: tuple[Expr, ...]
    negated: bool


@dataclass(frozen=True)
class Func:
    """An aggregate call such as ``COUNT(*)`` or ``SUM(age)``."""
    name: str  # upper-case aggregate name
    arg: Star | Expr


Expr = (
    Literal | Column | Star | Unary | Binary | IsNull | Like | InList | Func
)


# ---------------------------------------------------------------- statements

@dataclass(frozen=True)
class ColumnDef:
    name: str
    type: str  # "INTEGER" | "REAL" | "TEXT"
    primary_key: bool = False


@dataclass
class CreateTable:
    name: str
    columns: list[ColumnDef]
    if_not_exists: bool = False


@dataclass
class DropTable:
    name: str
    if_exists: bool = False


@dataclass
class Insert:
    table: str
    columns: list[str] | None  # None means "all columns, in order"
    rows: list[tuple[Expr, ...]]


@dataclass
class Update:
    table: str
    assignments: list[tuple[str, Expr]]
    where: Expr | None


@dataclass
class Delete:
    table: str
    where: Expr | None


@dataclass(frozen=True)
class SelectItem:
    expr: Expr
    alias: str | None = None


@dataclass(frozen=True)
class OrderItem:
    expr: Expr
    desc: bool = False


@dataclass
class TableRef:
    name: str
    alias: str | None = None

    @property
    def key(self) -> str:
        return self.alias or self.name


@dataclass
class JoinClause:
    table: TableRef
    on: Expr


@dataclass
class Select:
    items: list[SelectItem]
    table: TableRef
    distinct: bool = False
    joins: list[JoinClause] = field(default_factory=list)
    where: Expr | None = None
    group_by: list[Expr] = field(default_factory=list)
    order_by: list[OrderItem] = field(default_factory=list)
    limit: int | None = None
    offset: int = 0


Statement = Select | Insert | Update | Delete | CreateTable | DropTable
