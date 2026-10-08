"""Shared fixtures for the stoatdb test suite."""

from __future__ import annotations

import pytest

from stoatdb.engine import Database


@pytest.fixture
def db(tmp_path):
    """An empty in-file database, closed on teardown."""
    database = Database(str(tmp_path / "test.db"), create=True)
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def seeded(db):
    """A database holding a small employee/department dataset."""
    db.execute("CREATE TABLE employees (id INTEGER PRIMARY KEY, name TEXT, dept TEXT, salary REAL)")
    db.execute(
        "INSERT INTO employees (id, name, dept, salary) VALUES "
        "(1, 'ada', 'eng', 120.0), (2, 'grace', 'eng', 130.0), "
        "(3, 'alan', 'math', 110.0), (4, 'kurt', 'math', 140.0)"
    )
    db.execute("CREATE TABLE departments (code TEXT PRIMARY KEY, head TEXT)")
    db.execute(
        "INSERT INTO departments (code, head) VALUES ('eng', 'x'), ('math', 'y')"
    )
    return db
