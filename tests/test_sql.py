"""Tests for the SQL front-end: tokenizer and parser."""

from __future__ import annotations

import pytest

from stoatdb.errors import ParseError
from stoatdb.sql import ast_nodes as ast
from stoatdb.sql.lexer import tokenize
from stoatdb.sql.parser import parse_one


# --------------------------------------------------------------------- lexer

def test_keywords_case_insensitive():
    kinds = [(t.kind, t.value.upper()) for t in tokenize("select FROM where")]
    assert ("KW", "SELECT") == kinds[0]
    assert ("KW", "FROM") == kinds[1]
    assert ("KW", "WHERE") == kinds[2]


def test_string_with_escaped_quote():
    tokens = tokenize("'it''s fine'")
    assert tokens[0].kind == "STRING"
    assert tokens[0].value == "it's fine"


def test_comment_skipped():
    tokens = tokenize("SELECT 1 -- trailing comment\n, 2")
    values = [t.value for t in tokens if t.kind == "NUMBER"]
    assert values == ["1", "2"]


def test_two_char_operator_becomes_ne():
    tokens = tokenize("1 != 2")
    assert tokens[1].kind == "OP"
    assert tokens[1].value == "<>"


def test_bad_character_raises():
    with pytest.raises(ParseError):
        tokenize("SELECT #")


def test_unterminated_string_raises():
    with pytest.raises(ParseError):
        tokenize("'no end")


# -------------------------------------------------------------------- parser

def test_select_star():
    stmt = parse_one("SELECT * FROM t")
    assert isinstance(stmt, ast.Select)
    assert isinstance(stmt.items[0].expr, ast.Star)
    assert stmt.table.name == "t"


def test_select_where_order_limit():
    stmt = parse_one("SELECT a, b AS x FROM t WHERE a > 1 ORDER BY b DESC, a LIMIT 5 OFFSET 2")
    assert len(stmt.items) == 2
    assert stmt.items[1].alias == "x"
    assert isinstance(stmt.where, ast.Binary)
    assert stmt.where.op == ">"
    assert stmt.order_by[0].desc is True
    assert stmt.order_by[1].desc is False
    assert stmt.limit == 5
    assert stmt.offset == 2


def test_select_join_group():
    stmt = parse_one("SELECT dept, COUNT(*) FROM e JOIN d ON e.code = d.code GROUP BY dept")
    assert len(stmt.joins) == 1
    assert stmt.joins[0].table.name == "d"
    assert isinstance(stmt.items[1].expr, ast.Func)
    assert len(stmt.group_by) == 1


def test_select_distinct_and_qualified_columns():
    stmt = parse_one("SELECT DISTINCT t.a FROM t")
    assert stmt.distinct is True
    assert stmt.items[0].expr == ast.Column("t", "a")


def test_expression_precedence():
    stmt = parse_one("SELECT 1 + 2 * 3")
    expr = stmt.items[0].expr
    assert isinstance(expr, ast.Binary) and expr.op == "+"
    inner = expr.right
    assert isinstance(inner, ast.Binary) and inner.op == "*"


def test_logical_precedence_and_over_or():
    stmt = parse_one("SELECT 1 WHERE a OR b AND c")
    expr = stmt.where
    assert isinstance(expr, ast.Binary) and expr.op == "OR"
    assert isinstance(expr.right, ast.Binary) and expr.right.op == "AND"


def test_not_precedence():
    stmt = parse_one("SELECT 1 WHERE NOT a = 1 AND b = 2")
    expr = stmt.where
    assert isinstance(expr, ast.Binary) and expr.op == "AND"
    assert isinstance(expr.left, ast.Unary) and expr.left.op == "NOT"


def test_is_null_and_not_like():
    where = parse_one("SELECT 1 WHERE a IS NOT NULL AND b NOT LIKE 'x%'").where
    assert isinstance(where, ast.Binary) and where.op == "AND"
    assert isinstance(where.left, ast.IsNull) and where.left.negated
    assert isinstance(where.right, ast.Like) and where.right.negated


def test_in_list():
    where = parse_one("SELECT 1 WHERE a IN (1, 2, 3)").where
    assert isinstance(where, ast.InList)
    assert len(where.items) == 3
    assert where.negated is False


def test_insert_multi_row_with_columns():
    stmt = parse_one("INSERT INTO t (a, b) VALUES (1, 'x'), (2, 'y')")
    assert stmt.table == "t"
    assert stmt.columns == ["a", "b"]
    assert len(stmt.rows) == 2


def test_insert_bare():
    stmt = parse_one("INSERT INTO t VALUES (1)")
    assert stmt.columns is None


def test_create_table_with_pk():
    stmt = parse_one("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, name TEXT)")
    assert stmt.if_not_exists is True
    assert stmt.columns[0].primary_key is True
    assert stmt.columns[1].type == "TEXT"


def test_update_and_delete():
    upd = parse_one("UPDATE t SET a = a + 1, b = 'z' WHERE a < 3")
    assert isinstance(upd, ast.Update)
    assert len(upd.assignments) == 2
    dl = parse_one("DELETE FROM t")
    assert isinstance(dl, ast.Delete)
    assert dl.where is None


def test_drop_table_if_exists():
    stmt = parse_one("DROP TABLE IF EXISTS t")
    assert stmt.if_exists is True


def test_unary_minus_binds_tighter():
    stmt = parse_one("SELECT -2 * 3")
    expr = stmt.items[0].expr
    assert isinstance(expr, ast.Binary) and expr.op == "*"


def test_error_position_reported():
    with pytest.raises(ParseError) as excinfo:
        parse_one("SELECT FROM")
    assert excinfo.value.pos >= 7
