"""End-to-end tests: SQL in, results out, across reopens."""

from __future__ import annotations

import pytest

from stoatdb.engine import Database
from stoatdb.errors import ExecutionError, ParseError, StoatError


# ---------------------------------------------------------------------- DDL

def test_create_and_list_tables(db):
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
    assert db.table_names() == ["t"]
    assert "PRIMARY KEY" in db.schema_sql("t")


def test_create_duplicate_rejected(db):
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    with pytest.raises(ExecutionError):
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")


def test_create_if_not_exists_ok(db):
    db.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY)")
    db.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY)")  # no-op


def test_create_slice(db, tmp_path):
    with pytest.raises(ExecutionError):
        db.execute("CREATE TABLE t (a INTEGER, a TEXT)")


def test_drop_table(db, tmp_path):
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    db.execute("INSERT INTO t VALUES (1)")
    db.execute("DROP TABLE t")
    assert db.table_names() == []
    # recreate: rowids must restart cleanly
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    db.execute("INSERT INTO t VALUES (7)")
    assert db.execute("SELECT * FROM t").rows == [[7]]


def test_drop_missing(db, tmp_path):
    with pytest.raises(ExecutionError):
        db.execute("DROP TABLE nope")


# -------------------------------------------------------------------- INSERT

def test_insert_and_select_all(seeded):
    rows = seeded.execute("SELECT * FROM employees")
    assert rows.columns == ["id", "name", "dept", "salary"]
    assert [r[0] for r in rows.rows] == [1, 2, 3, 4]


def test_insert_without_pk_uses_rowids(db, tmp_path):
    db.execute("CREATE TABLE t (a TEXT)")
    db.execute("INSERT INTO t (a) VALUES ('x')")
    db.execute("INSERT INTO t (a) VALUES ('y')")
    result = db.execute("SELECT rowid_str_ignored FROM t".replace("rowid_str_ignored", "a"))
    assert result.rows == [["x"], ["y"]]


def test_type_enforcement(db):
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, w REAL, s TEXT)")
    db.execute("INSERT INTO t VALUES (1, 2, 'x')")            # int into REAL ok
    assert db.execute("SELECT w FROM t").rows == [[2.0]]
    with pytest.raises(ExecutionError):
        db.execute("INSERT INTO t VALUES (2, 'str', 'x')")     # str into REAL
    with pytest.raises(ExecutionError):
        db.execute("INSERT INTO t VALUES (3, 1.5, 5)")         # number into TEXT


def test_fk_none_but_column_check(db):
    db.execute("CREATE TABLE t (a INTEGER, b TEXT)")
    with pytest.raises(ExecutionError):
        db.execute("INSERT INTO t (a, nope) VALUES (1, 'x')")


def test_value_count_mismatch(db):
    db.execute("CREATE TABLE t (a INTEGER, b TEXT)")
    with pytest.raises(ExecutionError):
        db.execute("INSERT INTO t VALUES (1, 'x', 'extra')")


# --------------------------------------------------------------- primary key

def test_duplicate_pk_rejected(seeded):
    with pytest.raises(ExecutionError):
        seeded.execute("INSERT INTO employees (id, name, dept) VALUES (1, 'dupe', 'x')")


def test_pk_can_be_updated_if_unique(seeded):
    seeded.execute("UPDATE employees SET id = 99 WHERE id = 1")
    result = seeded.execute("SELECT id FROM employees ORDER BY id")
    assert [r[0] for r in result.rows] == [2, 3, 4, 99]


def test_pk_update_collides(seeded):
    with pytest.raises(ExecutionError):
        seeded.execute("UPDATE employees SET id = 2 WHERE id = 1")


# ---------------------------------------------------------------- projection

def test_projection_and_alias(seeded):
    rows = seeded.execute("SELECT name AS who, salary * 2 AS double FROM employees WHERE id = 2")
    assert rows.columns == ["who", "double"]
    assert rows.rows == [["grace", 260]]


def test_arithmetic_and_precedence(seeded):
    got = seeded.execute("SELECT 1 + 2 * 3, (1 + 2) * 3, 7 / 2, 1 / 0").rows
    assert got == [[7, 9, 3.5, None]]


def test_where_operators(seeded):
    def ids(q):
        return [r[0] for r in seeded.execute(q).rows]
    assert ids("SELECT id FROM employees WHERE salary >= 120 AND salary < 140") == [1, 2]
    assert ids("SELECT id FROM employees WHERE dept = 'math' OR id = 1") == [1, 3, 4]
    assert ids("SELECT id FROM employees WHERE NOT dept = 'eng'") == [3, 4]
    assert ids("SELECT id FROM employees WHERE id IN (2, 4)") == [2, 4]
    assert ids("SELECT id FROM employees WHERE name LIKE 'a%'") == [1, 3]
    assert ids("SELECT id FROM employees WHERE name LIKE '_race'") == [2]


def test_is_null(db):
    db.execute("CREATE TABLE t (a INTEGER, b TEXT)")
    db.execute("INSERT INTO t (a, b) VALUES (1, NULL)")
    db.execute("INSERT INTO t (a) VALUES (2)")
    assert db.execute("SELECT a FROM t WHERE b IS NULL").rows == [[1], [2]]
    assert db.execute("SELECT a FROM t WHERE b IS NOT NULL").rows == []

    # NULL propagation
    assert db.execute("SELECT a + b FROM t WHERE a = 1").rows == [[None]]


def test_distinct(seeded):
    rows = seeded.execute("SELECT DISTINCT dept FROM employees ORDER BY dept")
    assert rows.rows == [["eng"], ["math"]]


def test_order_by_multi_direction(seeded):
    rows = seeded.execute("SELECT dept, salary FROM employees ORDER BY dept ASC, salary DESC")
    assert rows.rows == [
        ["eng", 130.0], ["eng", 120.0], ["math", 140.0], ["math", 110.0],
    ]


def test_order_by_null_first_asc_last_desc(db):
    """SQLite-style ordering: NULLs sort first ascending, last descending."""
    db.execute("CREATE TABLE t (a INTEGER, b INTEGER)")
    db.execute("INSERT INTO t (a, b) VALUES (1, NULL), (2, 5), (3, 1)")
    asc = db.execute("SELECT a FROM t ORDER BY b ASC").rows
    desc = db.execute("SELECT a FROM t ORDER BY b DESC").rows
    assert asc == [[1], [3], [2]]
    assert desc == [[2], [3], [1]]


def test_limit_offset(seeded):
    rows = seeded.execute("SELECT id FROM employees ORDER BY id LIMIT 2 OFFSET 1")
    assert [r[0] for r in rows.rows] == [2, 3]


def test_order_by_ordinal(seeded):
    rows = seeded.execute("SELECT name, salary FROM employees ORDER BY 2 DESC LIMIT 1")
    assert rows.rows == [["kurt", 140.0]]


# ------------------------------------------------------------------- GROUP BY

def test_aggregates_whole_table(seeded):
    sql = (
        "SELECT COUNT(*), SUM(salary), AVG(salary), MIN(salary), MAX(salary) AS top"
        " FROM employees"
    )
    rows = seeded.execute(sql)
    assert rows.columns == ["COUNT(*)", "SUM(salary)", "AVG(salary)", "MIN(salary)", "top"]
    total = 120.0 + 130.0 + 110.0 + 140.0
    assert rows.rows == [[4, total, total / 4, 110.0, 140.0]]


def test_group_by_with_count(seeded):
    rows = seeded.execute(
        "SELECT dept, COUNT(*) AS n, AVG(salary) AS avg FROM employees GROUP BY dept ORDER BY dept"
    )
    assert rows.rows == [["eng", 2, 125.0], ["math", 2, 125.0]]


def test_aggregate_on_empty_group(db):
    db.execute("CREATE TABLE t (a INTEGER, b INTEGER)")
    db.execute("INSERT INTO t (a, b) VALUES (1, 10)")
    rows = db.execute("SELECT COUNT(*), SUM(b) FROM t WHERE a > 5")
    assert rows.rows == [[0, None]]  # COUNT totals over zero rows; SUM is NULL


def test_bare_column_without_group_rejected(seeded):
    with pytest.raises(ExecutionError):
        seeded.execute("SELECT dept, COUNT(*) FROM employees")


def test_group_by_column_with_where(seeded):
    rows = seeded.execute(
        "SELECT dept, SUM(salary) AS total FROM employees WHERE salary > 100 "
        "GROUP BY dept ORDER BY total DESC, dept ASC"
    )
    assert rows.rows == [["eng", 250.0], ["math", 250.0]]  # tie broken by dept ASC


# -------------------------------------------------------------------- UPDATE

def test_update_where_expression(seeded):
    n = seeded.execute("UPDATE employees SET salary = salary + 10 WHERE dept = 'eng'").rows_affected
    assert n == 2
    rows = seeded.execute("SELECT name, salary FROM employees WHERE dept = 'eng' ORDER BY name")
    assert rows.rows == [["ada", 130.0], ["grace", 140.0]]


def test_update_all(seeded):
    n = seeded.execute("UPDATE employees SET dept = 'core'").rows_affected
    assert n == 4
    rows = seeded.execute("SELECT DISTINCT dept FROM employees")
    assert rows.rows == [["core"]]


def test_update_nulls_and_coercion(seeded):
    seeded.execute("UPDATE employees SET salary = NULL WHERE id = 1")
    got = seeded.execute("SELECT salary FROM employees WHERE id = 1").rows
    assert got == [[None]]


# -------------------------------------------------------------------- DELETE

def test_delete_where(seeded):
    n = seeded.execute("DELETE FROM employees WHERE dept = 'eng'").rows_affected
    assert n == 2
    rows = seeded.execute("SELECT COUNT(*) FROM employees")
    assert rows.rows == [[2]]


def test_delete_all_then_reinsert(db):
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    for i in (1, 2, 3):
        db.execute(f"INSERT INTO t VALUES ({i})")
    db.execute("DELETE FROM t")
    assert db.execute("SELECT * FROM t").rows == []
    db.execute("INSERT INTO t VALUES (5)")
    assert db.execute("SELECT * FROM t").rows == [[5]]


# --------------------------------------------------------------------- JOIN

def test_inner_join_hash_path(seeded):
    rows = seeded.execute(
        "SELECT e.name, d.head FROM employees AS e JOIN departments AS d ON e.dept = d.code "
        "ORDER BY e.name"
    )
    assert rows.rows == [["ada", "x"], ["alan", "y"], ["grace", "x"], ["kurt", "y"]]


def test_join_nested_loop_fallback(seeded):
    # ON references only the left side, so every department pairs with any
    # employee matching the predicate: 3 matching employees x 2 departments
    rows = seeded.execute(
        "SELECT COUNT(*) AS n FROM employees e JOIN departments d ON e.salary > 115"
    )
    assert rows.rows == [[6]]


def test_join_unknown_column(seeded):
    with pytest.raises(ExecutionError):
        seeded.execute(
            "SELECT * FROM employees e JOIN departments d ON e.dept = d.missing"
        )


def test_qualified_and_unqualified_columns(seeded):
    rows = seeded.execute("SELECT e.id FROM employees e WHERE e.id = 1")
    assert rows.rows == [[1]]
    # 'id' exists only in employees, so the unqualified form is unambiguous
    rows = seeded.execute("SELECT id FROM employees e JOIN departments d ON e.dept = d.code")
    assert len(rows.rows) == 4
    # a truly unknown column still errors
    with pytest.raises(ExecutionError):
        seeded.execute("SELECT nosuch FROM employees e JOIN departments d ON e.dept = d.code")


# ----------------------------------------------------------------- pagination

def test_select_off_end(seeded):
    rows = seeded.execute("SELECT id FROM employees ORDER BY id LIMIT 5 OFFSET 3")
    assert [r[0] for r in rows.rows] == [4]


# ---------------------------------------------------------------- persistence

def test_rows_survive_reopen(tmp_path):
    path = str(tmp_path / "persist.db")
    db = Database(path, create=True)
    db.execute("CREATE TABLE big (id INTEGER PRIMARY KEY, blob TEXT)")
    for i in range(200):
        db.execute(f"INSERT INTO big VALUES ({i}, 'payload-{'x' * 40}-{i}')")
    db.close()

    db2 = Database(path)
    assert db2.execute("SELECT COUNT(*) FROM big").rows == [[200]]
    assert db2.execute("SELECT blob FROM big WHERE id = 123").rows == [
        ["payload-" + "x" * 40 + "-123"]
    ]
    # and it stays consistent after more writes
    db2.execute("UPDATE big SET blob = 'updated' WHERE id = 5")
    db2.close()
    db3 = Database(path)
    assert db3.execute("SELECT blob FROM big WHERE id = 5").rows == [["updated"]]
    db3.close()


def test_many_rows_trigger_splits(tmp_path):
    path = str(tmp_path / "deep.db")
    db = Database(path, create=True)
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, pad TEXT)")
    for i in range(500):
        db.execute(f"INSERT INTO t VALUES ({i}, '{'p' * 60}{i}')")
    rows = db.execute("SELECT COUNT(*), MAX(id) FROM t")
    assert rows.rows == [[500, 499]]
    assert db.execute("SELECT pad FROM t WHERE id = 250").rows == [["p" * 60 + "250"]]
    db.close()
    db2 = Database(path)
    assert db2.execute("SELECT COUNT(*) FROM t").rows == [[500]]
    db2.close()


# --------------------------------------------------------------------- errors

def test_parse_errors_surface(db, tmp_path):
    with pytest.raises(ParseError):
        db.execute("SELEC 1")
    with pytest.raises(ParseError):
        db.execute("SELECT 1 extra junk here")


def test_stoat_base_catches_everything(db, tmp_path):
    with pytest.raises(StoatError):
        db.execute("SELECT FROM x")
    with pytest.raises(StoatError):
        db.execute("SELECT * FROM missing_table")


# ----------------------------------------------------- primary key fast path

def test_pk_point_lookup(db):
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
    db.execute("INSERT INTO t VALUES (1, 'a'), (2, 'b'), (3, 'c')")
    assert db.execute("SELECT name FROM t WHERE id = 2").rows == [["b"]]
    # key absent -> empty, not a scan over everything
    assert db.execute("SELECT name FROM t WHERE id = 99").rows == []


def test_pk_fast_path_with_extra_predicates(db):
    """The fast path narrows candidates; the full WHERE still applies."""
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, score REAL)")
    db.execute("INSERT INTO t VALUES (1, 10.0), (2, 20.0), (3, 30.0)")
    got = db.execute("SELECT id FROM t WHERE id = 3 AND score > 100").rows
    assert got == []
    got = db.execute("SELECT id FROM t WHERE id = 3 AND score > 25").rows
    assert got == [[3]]


def test_non_pk_equality_still_correct(db):
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
    db.execute("INSERT INTO t VALUES (1, 'a'), (2, 'a')")
    assert db.execute("SELECT id FROM t WHERE name = 'a'").rows == [[1], [2]]


def test_qualified_pk_and_text_pk(db):
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
    db.execute("INSERT INTO t VALUES (1, 'a')")
    assert db.execute("SELECT id FROM t WHERE t.id = 1").rows == [[1]]

    db.execute("CREATE TABLE u (name TEXT PRIMARY KEY, n INTEGER)")
    db.execute("INSERT INTO u VALUES ('x', 1), ('y', 2)")
    assert db.execute("SELECT n FROM u WHERE name = 'y'").rows == [[2]]
