# stoatdb

[![CI](https://github.com/sandeep780049/stoatdb/actions/workflows/ci.yml/badge.svg)](https://github.com/sandeep780049/stoatdb/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.12%20%7C%203.14-blue)](https://www.python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

A small relational database engine written from scratch in pure Python —
no dependencies, no bindings, no borrowed code. SQL in, durable pages out.

stoatdb is a real storage engine, not an in-memory dict with a SQL skin:
rows live in on-disk B+trees, tables live in a paged file format, and every
statement is fsynced before the next one runs.

```
$ stoatdb demo.db -c "CREATE TABLE dept (id INTEGER PRIMARY KEY, name TEXT); CREATE TABLE emp (id INTEGER PRIMARY KEY, name TEXT, salary REAL, dept_id INTEGER); INSERT INTO dept VALUES (1, 'engineering'), (2, 'research'); INSERT INTO emp VALUES (1, 'ada', 120.5, 1), (2, 'grace', 135.0, 1), (3, 'alan', 110.0, 2); SELECT d.name, COUNT(*) AS headcount, AVG(e.salary) AS avg_salary FROM emp e JOIN dept d ON e.dept_id = d.id GROUP BY d.name ORDER BY avg_salary DESC;"
ok
ok
2 row(s) affected
3 row(s) affected
+-------------+-----------+------------+
| name        | headcount | avg_salary |
+-------------+-----------+------------+
| engineering | 2         | 127.75     |
| research    | 1         | 110.0      |
+-------------+-----------+------------+
2 row(s)

$ stoatdb demo.db
stoatdb 0.1.0 — type .help for help
stoat> SELECT name FROM emp WHERE salary > 115 ORDER BY salary DESC LIMIT 2;
+-------+
| name  |
+-------+
| grace |
| ada   |
+-------+
2 row(s)
stoat>
```

## Why

Most "build a database" projects stop at a dict. stoatdb goes one level
deeper: it owns a file format, grows and splits B+tree pages, reuses freed
pages through a free list, and survives process restarts. It is small
enough to read in an afternoon — about 2,200 lines across five modules —
but every layer is the real thing:

| Layer | File | What it does |
|---|---|---|
| SQL front-end | `sql/lexer.py`, `sql/parser.py` | hand-written tokenizer and recursive-descent parser with a proper precedence ladder |
| Executor | `engine.py` | FROM → JOIN → WHERE → GROUP BY → projection → DISTINCT → ORDER BY → LIMIT pipeline, hash joins, aggregate evaluation |
| Storage | `storage/pager.py`, `storage/btree.py` | 4 KiB pages, on-disk B+tree with page splits and a free list, fsync-per-statement durability |

## SQL support

- `CREATE TABLE` / `DROP TABLE [IF EXISTS]`, with `INTEGER`/`REAL`/`TEXT`
  columns and optional `PRIMARY KEY`
- `INSERT INTO ... VALUES` (single and multi-row), `UPDATE ... SET`,
  `DELETE FROM`
- `SELECT` with expressions, aliases, `DISTINCT`, `LIMIT`/`OFFSET`,
  ordinals in `ORDER BY`, and FROM-less selects (`SELECT 1 + 2`)
- `WHERE` with `AND` / `OR` / `NOT`, comparisons, `IS [NOT] NULL`,
  `[NOT] LIKE` (`%` and `_` wildcards), `[NOT] IN (...)`
- `JOIN ... ON` equality joins (executed as hash joins) plus arbitrary
  nested-loop joins
- Aggregates `COUNT`, `SUM`, `AVG`, `MIN`, `MAX` with `GROUP BY`, applied
  over plain expressions as well as columns

NULL follows SQL intuition: it propagates through arithmetic, compares
unequal to everything, sorts first ascending and last descending, and
`COUNT(*)` counts it while `SUM` skips it.

## Storage format

A stoatdb file is a flat sequence of 4096-byte pages.

- **Page 0** is the header: 8-byte magic, format version, page count,
  free-list head, catalog head.
- **The catalog** is a JSON document chained across pages; it records every
  table's columns, types, B+tree root page and next rowid.
- **Table data** lives in B+trees. A leaf page starts with the bytes `LF`
  followed by a next-page pointer and an entry count; an internal page starts
  with `IT`, a child count, and separator keys. Inserts split full pages on
  the way down; deleted pages go back to the free list.

`INTEGER` primary keys double as B-tree keys, so a query like
`WHERE id = 42` is a tree seek, not a scan — on a 20,000-row table a
primary-key lookup takes ~2 ms against ~260 ms for a full scan. Non-integer
primary keys use rowids plus a duplicate check on insert.

## Durability

Every statement runs under autocommit: dirty pages are written, the header
is updated, and the file is fsynced before the next statement starts. A
process crash between statements loses nothing that was acknowledged.

## CLI

```
stoatdb file.db              # interactive REPL (.tables, .schema <t>, .help, .quit)
stoatdb file.db -c "SQL; ..."  # run statements and exit
```

## Numbers

Measured on a laptop, Python 3.14, 20,000-row table:

| Operation | Time |
|---|---|
| Insert 20,000 rows (fsync each) | ~44 s (~455 rows/s) |
| Full-scan `COUNT(*) + AVG` | ~260 ms |
| Primary-key point lookup | ~2 ms |

The insert rate is fsync-bound by design; batch a script's statements into
one process and the page cache keeps the tree traversals off the disk.

## Try it

```
pip install -e ".[dev]"
pytest
stoatdb scratch.db
```

## Roadmap

- [ ] B+tree coalescing on delete (splits exist; merges are next)
- [ ] Secondary indexes
- [ ] Multi-statement transactions with rollback
- [ ] WAL-based durability so fsync amortizes across statements

## License

MIT — see [LICENSE](LICENSE).
