"""Recursive-descent parser for the stoatdb SQL dialect.

Grammar (precedence low to high)::

    statement  := select | insert | update | delete | create | drop
    expr       := or_expr
    or_expr    := and_expr (OR and_expr)*
    and_expr   := not_expr (AND not_expr)*
    not_expr   := NOT not_expr | comparison
    comparison := additive ( (=|<>|<|<=|>|>=) additive
                           | IS [NOT] NULL
                           | [NOT] LIKE additive
                           | [NOT] IN ( expr, ... ) )?
    additive   := multiplicative ((+|-) multiplicative)*
    multiplicative := unary ((*|/) unary)*
    unary      := - unary | primary
    primary    := NUMBER | STRING | NULL | func(args) | column | ( expr )

Every construct records enough source position context for decent error
messages via :class:`~stoatdb.errors.ParseError`.
"""

from __future__ import annotations


from ..errors import ParseError
from . import ast_nodes as ast
from .lexer import AGGREGATES, Token, tokenize


class Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.tokens = tokenize(text)
        self.pos = 0

    # ------------------------------------------------------------- utilities

    def peek(self) -> Token:
        return self.tokens[self.pos]

    def advance(self) -> Token:
        tok = self.tokens[self.pos]
        if tok.kind != "EOF":
            self.pos += 1
        return tok

    def error(self, message: str) -> ParseError:
        return ParseError(message, self.peek().pos, self.text)

    def accept_kw(self, *names: str) -> Token | None:
        if self.peek().is_kw(*names):
            return self.advance()
        return None

    def expect_kw(self, name: str) -> Token:
        tok = self.peek()
        if not tok.is_kw(name):
            raise self.error(f"expected {name}")
        return self.advance()

    def accept_op(self, *ops: str) -> Token | None:
        tok = self.peek()
        if tok.kind == "OP" and tok.value in ops:
            return self.advance()
        return None

    def expect_op(self, op: str) -> Token:
        tok = self.peek()
        if tok.kind != "OP" or tok.value != op:
            raise self.error(f"expected {op!r}")
        return self.advance()

    def expect_ident(self, what: str = "identifier") -> str:
        tok = self.peek()
        if tok.kind != "IDENT":
            raise self.error(f"expected {what}")
        return self.advance().value

    # ------------------------------------------------------------- statements

    def parse_statement(self) -> ast.Statement:
        tok = self.peek()
        if tok.is_kw("SELECT"):
            return self.parse_select()
        if tok.is_kw("INSERT"):
            return self.parse_insert()
        if tok.is_kw("CREATE"):
            return self.parse_create()
        if tok.is_kw("UPDATE"):
            return self.parse_update()
        if tok.is_kw("DELETE"):
            return self.parse_delete()
        if tok.is_kw("DROP"):
            return self.parse_drop()
        raise self.error("expected a statement (SELECT, INSERT, CREATE, ...)")

    def parse_script(self) -> list[ast.Statement]:
        """Parse a sequence of ``;``-separated statements."""
        statements: list[ast.Statement] = []
        while self.peek().kind != "EOF":
            statements.append(self.parse_statement())
            while self.accept_op(";"):
                pass
        return statements

    # ----------------------------------------------------------------- select

    def parse_select(self) -> ast.Select:
        self.expect_kw("SELECT")
        distinct = bool(self.accept_kw("DISTINCT"))
        items = self.parse_select_items()
        if self.accept_kw("FROM"):
            table = self.parse_table_ref()
        else:
            # FROM-less expression select: SELECT 1+2, SELECT 2*3, ...
            table = ast.TableRef("", None)
        joins: list[ast.JoinClause] = []
        while self.accept_kw("JOIN"):
            joined = self.parse_table_ref()
            self.expect_kw("ON")
            joins.append(ast.JoinClause(table=joined, on=self.parse_expr()))
        where = self.parse_expr() if self.accept_kw("WHERE") else None
        group_by: list[ast.Expr] = []
        if self.accept_kw("GROUP"):
            self.expect_kw("BY")
            group_by.append(self.parse_expr())
            while self.accept_op(","):
                group_by.append(self.parse_expr())
        order_by = self.parse_order_by()
        limit: int | None = None
        offset = 0
        if self.accept_kw("LIMIT"):
            limit = self.parse_int("LIMIT")
            if self.accept_kw("OFFSET"):
                offset = self.parse_int("OFFSET")
        return ast.Select(
            items=items, table=table, distinct=distinct, joins=joins,
            where=where, group_by=group_by, order_by=order_by,
            limit=limit, offset=offset,
        )

    def parse_select_items(self) -> list[ast.SelectItem]:
        if self.accept_op("*"):
            return [ast.SelectItem(ast.Star())]
        items = [self.parse_select_item()]
        while self.accept_op(","):
            items.append(self.parse_select_item())
        return items

    def parse_select_item(self) -> ast.SelectItem:
        expr = self.parse_expr()
        if self.accept_kw("AS"):
            return ast.SelectItem(expr, self.expect_ident("alias"))
        if self.peek().kind == "IDENT":
            return ast.SelectItem(expr, self.advance().value)
        return ast.SelectItem(expr)

    def parse_table_ref(self) -> ast.TableRef:
        name = self.expect_ident("table name")
        alias = None
        if self.accept_kw("AS"):
            alias = self.expect_ident("alias")
        elif self.peek().kind == "IDENT":
            alias = self.advance().value
        return ast.TableRef(name, alias)

    def parse_order_by(self) -> list[ast.OrderItem]:
        if not self.accept_kw("ORDER"):
            return []
        self.expect_kw("BY")
        items = [self.parse_order_item()]
        while self.accept_op(","):
            items.append(self.parse_order_item())
        return items

    def parse_order_item(self) -> ast.OrderItem:
        expr = self.parse_expr()
        desc = False
        if self.accept_kw("DESC"):
            desc = True
        else:
            self.accept_kw("ASC")
        return ast.OrderItem(expr, desc)

    def parse_int(self, clause: str) -> int:
        tok = self.peek()
        if tok.kind != "NUMBER" or not tok.value.isdigit():
            raise self.error(f"{clause} expects a non-negative integer")
        return int(self.advance().value)

    # ----------------------------------------------------------------- insert

    def parse_insert(self) -> ast.Insert:
        self.expect_kw("INSERT")
        self.expect_kw("INTO")
        table = self.expect_ident("table name")
        columns: list[str] | None = None
        if self.accept_op("("):
            columns = [self.expect_ident("column name")]
            while self.accept_op(","):
                columns.append(self.expect_ident("column name"))
            self.expect_op(")")
        self.expect_kw("VALUES")
        rows = [self.parse_value_tuple()]
        while self.accept_op(","):
            rows.append(self.parse_value_tuple())
        return ast.Insert(table, columns, rows)

    def parse_value_tuple(self) -> tuple[ast.Expr, ...]:
        self.expect_op("(")
        values = [self.parse_expr()]
        while self.accept_op(","):
            values.append(self.parse_expr())
        self.expect_op(")")
        return tuple(values)

    # ----------------------------------------------------------------- create

    def parse_create(self) -> ast.CreateTable:
        self.expect_kw("CREATE")
        self.expect_kw("TABLE")
        if_not_exists = bool(self.accept_kw("IF")) and self._expect_exists()
        name = self.expect_ident("table name")
        self.expect_op("(")
        columns = [self.parse_column_def()]
        while self.accept_op(","):
            columns.append(self.parse_column_def())
        self.expect_op(")")
        return ast.CreateTable(name, columns, if_not_exists)

    def _expect_exists(self) -> bool:
        self.expect_kw("NOT")
        self.expect_kw("EXISTS")
        return True

    def parse_column_def(self) -> ast.ColumnDef:
        name = self.expect_ident("column name")
        tok = self.peek()
        if not tok.is_kw("INTEGER", "REAL", "TEXT"):
            raise self.error("column type must be INTEGER, REAL or TEXT")
        type_name = self.advance().value.upper()
        pk = False
        if self.accept_kw("PRIMARY"):
            self.expect_kw("KEY")
            pk = True
        return ast.ColumnDef(name, type_name, pk)

    # ------------------------------------------------------- update / delete

    def parse_update(self) -> ast.Update:
        self.expect_kw("UPDATE")
        table = self.expect_ident("table name")
        self.expect_kw("SET")
        assignments = [self.parse_assignment()]
        while self.accept_op(","):
            assignments.append(self.parse_assignment())
        where = self.parse_expr() if self.accept_kw("WHERE") else None
        return ast.Update(table, assignments, where)

    def parse_assignment(self) -> tuple[str, ast.Expr]:
        name = self.expect_ident("column name")
        self.expect_op("=")
        return name, self.parse_expr()

    def parse_delete(self) -> ast.Delete:
        self.expect_kw("DELETE")
        self.expect_kw("FROM")
        table = self.expect_ident("table name")
        where = self.parse_expr() if self.accept_kw("WHERE") else None
        return ast.Delete(table, where)

    def parse_drop(self) -> ast.DropTable:
        self.expect_kw("DROP")
        self.expect_kw("TABLE")
        if_exists = bool(self.accept_kw("IF"))
        if if_exists:
            self.expect_kw("EXISTS")
        name = self.expect_ident("table name")
        return ast.DropTable(name, if_exists)

    # ------------------------------------------------------------ expressions

    def parse_expr(self) -> ast.Expr:
        return self.parse_or()

    def parse_or(self) -> ast.Expr:
        left = self.parse_and()
        while self.accept_kw("OR"):
            left = ast.Binary("OR", left, self.parse_and())
        return left

    def parse_and(self) -> ast.Expr:
        left = self.parse_not()
        while self.accept_kw("AND"):
            left = ast.Binary("AND", left, self.parse_not())
        return left

    def parse_not(self) -> ast.Expr:
        if self.accept_kw("NOT"):
            return ast.Unary("NOT", self.parse_not())
        return self.parse_comparison()

    def parse_comparison(self) -> ast.Expr:
        left = self.parse_additive()
        op_tok = self.accept_op("=", "<>", "<", "<=", ">", ">=")
        if op_tok:
            return ast.Binary(op_tok.value, left, self.parse_additive())
        if self.accept_kw("IS"):
            negated = bool(self.accept_kw("NOT"))
            self.expect_kw("NULL")
            return ast.IsNull(left, negated)
        negated = bool(self.accept_kw("NOT"))
        if self.accept_kw("LIKE"):
            return ast.Like(left, self.parse_additive(), negated)
        if self.accept_kw("IN"):
            self.expect_op("(")
            items = [self.parse_expr()]
            while self.accept_op(","):
                items.append(self.parse_expr())
            self.expect_op(")")
            return ast.InList(left, tuple(items), negated)
        if negated:
            raise self.error("expected LIKE or IN after NOT")
        return left

    def parse_additive(self) -> ast.Expr:
        left = self.parse_multiplicative()
        while True:
            op = self.accept_op("+", "-")
            if not op:
                return left
            left = ast.Binary(op.value, left, self.parse_multiplicative())

    def parse_multiplicative(self) -> ast.Expr:
        left = self.parse_unary()
        while True:
            op = self.accept_op("*", "/")
            if not op:
                return left
            left = ast.Binary(op.value, left, self.parse_unary())

    def parse_unary(self) -> ast.Expr:
        if self.accept_op("-"):
            return ast.Unary("-", self.parse_unary())
        return self.parse_primary()

    def parse_primary(self) -> ast.Expr:
        tok = self.peek()
        if tok.kind == "NUMBER":
            self.advance()
            text = tok.value
            value: float | int = float(text) if "." in text else int(text)
            return ast.Literal(value)
        if tok.kind == "STRING":
            self.advance()
            return ast.Literal(tok.value)
        if tok.is_kw("NULL"):
            self.advance()
            return ast.Literal(None)
        if tok.is_kw("NOT"):
            self.advance()
            return ast.Unary("NOT", self.parse_not())
        if tok.kind == "OP" and tok.value == "(":
            self.advance()
            inner = self.parse_expr()
            self.expect_op(")")
            return inner
        if tok.kind == "IDENT":
            self.advance()
            if tok.value.upper() in AGGREGATES and self.accept_op("("):
                return self.parse_aggregate(tok.value.upper())
            return self.parse_column_suffix(tok.value)
        raise self.error("expected an expression")

    def parse_aggregate(self, name: str) -> ast.Func:
        if self.accept_op("*"):
            if name != "COUNT":
                raise self.error(f"{name}(*) is not supported")
            arg: ast.Expr | ast.Star = ast.Star()
        else:
            arg = self.parse_expr()
        self.expect_op(")")
        return ast.Func(name, arg)

    def parse_column_suffix(self, first: str) -> ast.Expr:
        if self.accept_op("."):
            return ast.Column(first, self.expect_ident("column name"))
        return ast.Column(None, first)


def parse_one(sql: str) -> ast.Statement:
    """Parse a single statement; trailing text (besides ``;``) is an error."""
    parser = Parser(sql)
    statement = parser.parse_statement()
    while parser.accept_op(";"):
        pass
    if parser.peek().kind != "EOF":
        raise parser.error("unexpected input after statement")
    return statement


def parse_script(sql: str) -> list[ast.Statement]:
    return Parser(sql).parse_script()
