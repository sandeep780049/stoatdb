"""Query execution: catalog management, DDL/DML and the SELECT pipeline.

The pipeline for a SELECT is::

    FROM/JOIN -> WHERE -> GROUP BY/aggregates -> projection
             -> DISTINCT -> ORDER BY -> LIMIT/OFFSET

Rows are stored in a per-table :class:`~stoatdb.storage.btree.BTree` keyed by
rowid; a row's payload is its column values JSON-encoded in table order.
Statements autocommit: pages are flushed (fsync) after every statement.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from collections.abc import Sequence

from .errors import ExecutionError
from .sql import ast_nodes as ast
from .sql.parser import parse_one
from .storage.btree import BTree
from .storage.pager import Pager

Value = int | float | str | None
Binding = dict[str, tuple]  # table key -> row tuple


@dataclass
class Result:
    """What one statement returns: a table for SELECT, counts otherwise."""

    columns: list[str] = field(default_factory=list)
    rows: list[list[Value]] = field(default_factory=list)
    rows_affected: int = 0


@dataclass
class _Table:
    name: str
    columns: list[str]
    types: list[str]
    pk_index: int | None
    root: int | None
    next_rowid: int

    @property
    def pk_is_stored_key(self) -> bool:
        """True when the primary key column doubles as the B-tree key."""
        return self.pk_index is not None and self.types[self.pk_index] == "INTEGER"

    def to_json(self) -> dict:
        return {
            "columns": [
                [name, self.types[i], i == self.pk_index]
                for i, name in enumerate(self.columns)
            ],
            "root": self.root,
            "next_rowid": self.next_rowid,
        }

    @staticmethod
    def from_json(name: str, data: dict) -> _Table:
        columns = [c[0] for c in data["columns"]]
        types = [c[1] for c in data["columns"]]
        pk_indexes = [i for i, c in enumerate(data["columns"]) if c[2]]
        return _Table(name, columns, types, pk_indexes[0] if pk_indexes else None,
                      data["root"], data["next_rowid"])


class _Resolver:
    """Maps column references to ``(table_key, index)`` within one query."""

    def __init__(self, tables: list[tuple[str, list[str]]]) -> None:
        self._by_table: dict[str, list[str]] = dict(tables)
        self._qualified: dict[tuple[str, str], int] = {}
        self._unqualified: dict[str, list[tuple[str, int]]] = {}
        for tkey, colnames in tables:
            for idx, col in enumerate(colnames):
                self._qualified[(tkey, col)] = idx
                self._unqualified.setdefault(col, []).append((tkey, idx))

    def resolve(self, col: ast.Column) -> tuple[str, int]:
        if col.table is not None:
            if col.table not in self._by_table:
                raise ExecutionError(f"no such table or alias: {col.table!r}")
            try:
                return col.table, self._qualified[(col.table, col.name)]
            except KeyError:
                raise ExecutionError(
                    f"table {col.table!r} has no column {col.name!r}"
                ) from None
        matches = self._unqualified.get(col.name, [])
        if not matches:
            raise ExecutionError(f"no such column: {col.name!r}")
        if len(matches) > 1:
            raise ExecutionError(f"ambiguous column: {col.name!r}; qualify it")
        return matches[0]

    def has_aggregate_allowed(self, expr: ast.Expr, group_by: Sequence[ast.Expr]) -> None:
        """Reject aggregates outside the select list, and non-grouped columns
        inside an aggregate query's select list."""
        if _contains_func(expr):
            raise ExecutionError("aggregate functions are only allowed in the select list")
        refs = _column_refs(expr)
        for ref in refs:
            if not any(_same_expr(ref, g) for g in group_by):
                name = ref.name if isinstance(ref, ast.Column) else str(ref)
                raise ExecutionError(f"column {name!r} must appear in GROUP BY")


def _contains_func(expr: Any) -> bool:
    if isinstance(expr, ast.Func):
        return True
    if isinstance(expr, ast.Binary):
        return _contains_func(expr.left) or _contains_func(expr.right)
    if isinstance(expr, ast.Unary):
        return _contains_func(expr.operand)
    if isinstance(expr, ast.IsNull):
        return _contains_func(expr.operand)
    if isinstance(expr, ast.Like):
        return _contains_func(expr.operand) or _contains_func(expr.pattern)
    if isinstance(expr, ast.InList):
        return _contains_func(expr.operand) or any(_contains_func(e) for e in expr.items)
    return False


def _column_refs(expr: Any) -> list[ast.Column]:
    out: list[ast.Column] = []

    def walk(node: Any) -> None:
        if isinstance(node, ast.Column):
            out.append(node)
        elif isinstance(node, ast.Binary):
            walk(node.left)
            walk(node.right)
        elif isinstance(node, ast.Unary):
            walk(node.operand)
        elif isinstance(node, ast.IsNull):
            walk(node.operand)
        elif isinstance(node, ast.Like):
            walk(node.operand)
            walk(node.pattern)
        elif isinstance(node, ast.InList):
            walk(node.operand)
            for item in node.items:
                walk(item)
        elif isinstance(node, ast.Func) and not isinstance(node.arg, ast.Star):
            walk(node.arg)

    walk(expr)
    return out


def _same_expr(a: Any, b: Any) -> bool:
    """Structural equality for AST fragments (dataclass eq does the work)."""
    return type(a) is type(b) and a == b


# ------------------------------------------------------------------ evaluation

def _type_rank(v: Value) -> int:
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        return 1
    return 2


def _sort_key(v: Value) -> tuple[int, Any]:
    return (_type_rank(v), v if v is not None else 0)


def compare(a: Value, b: Value) -> int:
    """Three-way compare with SQL-ish total ordering: NULL < numbers < text."""
    ra, rb = _type_rank(a), _type_rank(b)
    if ra != rb:
        return -1 if ra < rb else 1
    if ra == 0:
        return 0
    if a < b:
        return -1
    if a > b:
        return 1
    return 0


def eval_expr(expr: ast.Expr, binding: Binding, resolver: _Resolver) -> Value:
    """Evaluate *expr* against one joined row binding."""
    if isinstance(expr, ast.Literal):
        return expr.value
    if isinstance(expr, ast.Column):
        tkey, idx = resolver.resolve(expr)
        return binding[tkey][idx]
    if isinstance(expr, ast.Star):
        raise ExecutionError("* is not valid in this context")
    if isinstance(expr, ast.Func):
        raise ExecutionError(f"aggregate {expr.name} is not valid in this context")
    if isinstance(expr, ast.Unary):
        if expr.op == "NOT":
            return not _truthy(eval_expr(expr.operand, binding, resolver))
        v = eval_expr(expr.operand, binding, resolver)
        if v is None:
            return None
        _require_number(v, "unary -")
        return -v
    if isinstance(expr, ast.Binary):
        return _eval_binary(expr, binding, resolver)
    if isinstance(expr, ast.IsNull):
        v = eval_expr(expr.operand, binding, resolver)
        return (v is not None) if expr.negated else (v is None)
    if isinstance(expr, ast.Like):
        v = eval_expr(expr.operand, binding, resolver)
        pattern = eval_expr(expr.pattern, binding, resolver)
        if v is None or pattern is None:
            return False
        if not isinstance(v, str) or not isinstance(pattern, str):
            raise ExecutionError("LIKE requires text operands")
        matched = _like_regex(pattern).fullmatch(v) is not None
        return (not matched) if expr.negated else matched
    if isinstance(expr, ast.InList):
        v = eval_expr(expr.operand, binding, resolver)
        if v is None:
            return False
        found = False
        for item in expr.items:
            candidate = eval_expr(item, binding, resolver)
            if candidate is not None and compare(v, candidate) == 0:
                found = True
                break
        return (not found) if expr.negated else found
    raise ExecutionError(f"cannot evaluate expression node {type(expr).__name__}")


def _eval_binary(expr: ast.Binary, binding: Binding, resolver: _Resolver) -> Value:
    if expr.op == "AND":
        return _truthy(eval_expr(expr.left, binding, resolver)) and \
            _truthy(eval_expr(expr.right, binding, resolver))
    if expr.op == "OR":
        return _truthy(eval_expr(expr.left, binding, resolver)) or \
            _truthy(eval_expr(expr.right, binding, resolver))
    left = eval_expr(expr.left, binding, resolver)
    right = eval_expr(expr.right, binding, resolver)
    if expr.op in ("=", "<>", "<", "<=", ">", ">="):
        if left is None or right is None:
            return False
        outcome = compare(left, right)
        return {
            "=": outcome == 0,
            "<>": outcome != 0,
            "<": outcome < 0,
            "<=": outcome <= 0,
            ">": outcome > 0,
            ">=": outcome >= 0,
        }[expr.op]
    if left is None or right is None:
        return None
    _require_number(left, expr.op)
    _require_number(right, expr.op)
    if expr.op == "+":
        return left + right
    if expr.op == "-":
        return left - right
    if expr.op == "*":
        return left * right
    if right == 0:
        return None  # SQL-style: division by zero yields NULL
    return left / right


def _require_number(v: Value, context: str) -> None:
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        raise ExecutionError(f"{context} requires numeric operands, got {v!r}")


def _truthy(v: Value) -> bool:
    return v is not None and v is not False and v != 0


def _like_regex(pattern: str) -> re.Pattern[str]:
    parts = ["^"]  # kept for clarity; fullmatch also anchors
    for ch in pattern:
        if ch == "%":
            parts.append(".*")
        elif ch == "_":
            parts.append(".")
        else:
            parts.append(re.escape(ch))
    parts.append("$")
    return re.compile("".join(parts), re.DOTALL)


# ------------------------------------------------------------------- aggregates

def _aggregate_value(func: ast.Func, rows: list[Binding],
                     resolver: _Resolver) -> Value:
    if isinstance(func.arg, ast.Star):
        if func.name != "COUNT":
            raise ExecutionError(f"{func.name}(*) is not supported")
        return len(rows)
    values = [eval_expr(func.arg, b, resolver) for b in rows]
    values = [v for v in values if v is not None]
    if func.name == "COUNT":
        return len(values)
    if not values:
        return None
    if func.name in ("SUM", "AVG"):
        for v in values:
            _require_number(v, func.name)
        total = sum(values)  # type: ignore[arg-type]
        return total if func.name == "SUM" else total / len(values)
    # MIN / MAX
    best = values[0]
    for v in values[1:]:
        c = compare(v, best)
        if (func.name == "MIN" and c < 0) or (func.name == "MAX" and c > 0):
            best = v
    return best


def expr_label(expr: ast.Expr) -> str:
    """Human-readable name for a select item without an explicit alias."""
    if isinstance(expr, ast.Column):
        return expr.name
    if isinstance(expr, ast.Func):
        if isinstance(expr.arg, ast.Star):
            return f"{expr.name}(*)"
        return f"{expr.name}({expr_label(expr.arg)})"
    if isinstance(expr, ast.Literal):
        return repr(expr.value)
    if isinstance(expr, ast.Binary):
        return f"{expr_label(expr.left)} {expr.op} {expr_label(expr.right)}"
    if isinstance(expr, ast.Star):
        return "*"
    return "expr"


# -------------------------------------------------------------------- database

class Database:
    """A stoatdb database bound to a single file."""

    def __init__(self, path: str, *, create: bool = False) -> None:
        self.pager = Pager(path, create=create)
        raw = self.pager.load_catalog()
        self._tables: dict[str, _Table] = {
            name: _Table.from_json(name, data)
            for name, data in raw.get("tables", {}).items()
        }

    # ------------------------------------------------------------------ public

    def execute(self, sql: str) -> Result:
        statement = parse_one(sql)
        try:
            result = self._run(statement)
        finally:
            self.pager.flush()  # autocommit at statement granularity
        return result

    def close(self) -> None:
        self.pager.close()

    def table_names(self) -> list[str]:
        return sorted(self._tables)

    def schema_sql(self, name: str) -> str:
        table = self._require_table(name)
        defs = []
        for i, col in enumerate(table.columns):
            piece = f"  {col} {table.types[i]}"
            if i == table.pk_index:
                piece += " PRIMARY KEY"
            defs.append(piece)
        return f"CREATE TABLE {name} (\n" + ",\n".join(defs) + "\n)"

    # ---------------------------------------------------------------- dispatch

    def _run(self, stmt: ast.Statement) -> Result:
        if isinstance(stmt, ast.Select):
            return self._exec_select(stmt)
        if isinstance(stmt, ast.Insert):
            return self._exec_insert(stmt)
        if isinstance(stmt, ast.Update):
            return self._exec_update(stmt)
        if isinstance(stmt, ast.Delete):
            return self._exec_delete(stmt)
        if isinstance(stmt, ast.CreateTable):
            return self._exec_create(stmt)
        if isinstance(stmt, ast.DropTable):
            return self._exec_drop(stmt)
        raise ExecutionError(f"unsupported statement {type(stmt).__name__}")

    # ----------------------------------------------------------------- catalog

    def _require_table(self, name: str) -> _Table:
        table = self._tables.get(name.lower())
        if table is None:
            raise ExecutionError(f"no such table: {name!r}")
        return table

    def _save_catalog(self) -> None:
        self.pager.save_catalog(
            {"tables": {name: t.to_json() for name, t in self._tables.items()}}
        )

    def _tree(self, table: _Table) -> BTree:
        return BTree(self.pager, table.root)

    def _exec_create(self, stmt: ast.CreateTable) -> Result:
        name = stmt.name.lower()
        if name in self._tables:
            if stmt.if_not_exists:
                return Result()
            raise ExecutionError(f"table {name!r} already exists")
        if not stmt.columns:
            raise ExecutionError("a table needs at least one column")
        seen: set[str] = set()
        pk_seen = 0
        for col in stmt.columns:
            if col.name.lower() in seen:
                raise ExecutionError(f"duplicate column {col.name!r}")
            seen.add(col.name.lower())
            if col.primary_key:
                pk_seen += 1
        if pk_seen > 1:
            raise ExecutionError("only one PRIMARY KEY column is allowed")
        table = _Table(
            name=name,
            columns=[c.name.lower() for c in stmt.columns],
            types=[c.type for c in stmt.columns],
            pk_index=next((i for i, c in enumerate(stmt.columns) if c.primary_key), None),
            root=None,
            next_rowid=1,
        )
        self._tables[name] = table
        self._save_catalog()
        return Result()

    def _exec_drop(self, stmt: ast.DropTable) -> Result:
        name = stmt.name.lower()
        table = self._tables.get(name)
        if table is None:
            if stmt.if_exists:
                return Result()
            raise ExecutionError(f"no such table: {name!r}")
        self._tree(table).free_all()
        del self._tables[name]
        self._save_catalog()
        return Result()

    # --------------------------------------------------------------------- DML

    def _coerce(self, value: Value, type_name: str, column: str) -> Value:
        if value is None:
            return None
        if type_name == "INTEGER":
            if type(value) is int:
                return value
            if type(value) is float and value.is_integer():
                return int(value)
            raise ExecutionError(f"column {column!r} is INTEGER, got {value!r}")
        if type_name == "REAL":
            if type(value) in (int, float):
                return float(value)
            raise ExecutionError(f"column {column!r} is REAL, got {value!r}")
        if type(value) is str:
            return value
        raise ExecutionError(f"column {column!r} is TEXT, got {value!r}")

    def _exec_insert(self, stmt: ast.Insert) -> Result:
        table = self._require_table(stmt.table)
        tree = self._tree(table)
        if stmt.columns is None:
            targets = list(range(len(table.columns)))
        else:
            targets = []
            for col in stmt.columns:
                lname = col.lower()
                if lname not in table.columns:
                    raise ExecutionError(f"table {table.name!r} has no column {col!r}")
                targets.append(table.columns.index(lname))
            if len(set(targets)) != len(targets):
                raise ExecutionError("duplicate column in INSERT list")

        resolver = _Resolver([(table.name, table.columns)])
        count = 0
        empty: Binding = {}
        for row_exprs in stmt.rows:
            values: list[Value] = [None] * len(table.columns)
            supplied: dict[int, Value] = {}
            for pos, expr in enumerate(row_exprs):
                if pos >= len(targets):
                    raise ExecutionError(
                        f"{len(row_exprs)} values for {len(targets)} columns"
                    )
                raw = eval_expr(expr, empty, resolver)
                idx = targets[pos]
                supplied[idx] = self._coerce(raw, table.types[idx], table.columns[idx])
            if len(row_exprs) != len(targets):
                raise ExecutionError(
                    f"{len(row_exprs)} values for {len(targets)} columns"
                )
            for idx, v in supplied.items():
                values[idx] = v

            if table.pk_is_stored_key:
                rowid = values[table.pk_index]  # type: ignore[assignment]
                if type(rowid) is not int:
                    raise ExecutionError(
                        f"INTEGER PRIMARY KEY {table.columns[table.pk_index]!r} must be an integer"
                    )
                if tree.get(rowid) is not None:
                    raise ExecutionError(f"duplicate primary key: {rowid}")
            else:
                rowid = table.next_rowid
                if table.pk_index is not None:
                    # TEXT primary key: enforced with a full-tree scan check
                    pk_val = values[table.pk_index]
                    for _k, existing in tree.scan():
                        if json.loads(existing)[table.pk_index] == pk_val:
                            raise ExecutionError(f"duplicate primary key: {pk_val}")
            payload = json.dumps(values, separators=(",", ":")).encode()
            tree.set(rowid, payload)
            if rowid >= table.next_rowid:
                table.next_rowid = rowid + 1
            count += 1
        table.root = tree.root
        self._save_catalog()
        return Result(rows_affected=count)

    def _exec_update(self, stmt: ast.Update) -> Result:
        table = self._require_table(stmt.table)
        tree = self._tree(table)
        resolver = _Resolver([(table.name, table.columns)])
        assignments: list[tuple[int, ast.Expr]] = []
        for col, expr in stmt.assignments:
            lname = col.lower()
            if lname not in table.columns:
                raise ExecutionError(f"table {table.name!r} has no column {col!r}")
            if _contains_func(expr):
                raise ExecutionError("aggregates are not allowed in UPDATE")
            assignments.append((table.columns.index(lname), expr))

        affected = 0
        decoded = [(key, json.loads(payload)) for key, payload in tree.scan()]
        for key, row in decoded:
            binding = {table.name: tuple(row)}
            if stmt.where is not None and not _truthy(
                eval_expr(stmt.where, binding, resolver)
            ):
                continue
            new_row = list(row)
            pk_changed = False
            for idx, expr in assignments:
                raw = eval_expr(expr, binding, resolver)
                coerced = self._coerce(raw, table.types[idx], table.columns[idx])
                if idx == table.pk_index and coerced != row[idx]:
                    pk_changed = True
                    if table.pk_is_stored_key and type(coerced) is not int:
                        raise ExecutionError(
                            f"INTEGER PRIMARY KEY {table.columns[idx]!r} must be an integer"
                        )
                new_row[idx] = coerced
            if pk_changed:
                if tree.get(new_row[table.pk_index]) is not None:
                    raise ExecutionError(f"duplicate primary key: {new_row[table.pk_index]}")
                tree.delete(key)
            payload = json.dumps(new_row, separators=(",", ":")).encode()
            tree.set(new_row[table.pk_index] if pk_changed else key, payload)
            affected += 1
        table.root = tree.root
        self._save_catalog()
        return Result(rows_affected=affected)

    def _exec_delete(self, stmt: ast.Delete) -> Result:
        table = self._require_table(stmt.table)
        tree = self._tree(table)
        resolver = _Resolver([(table.name, table.columns)])
        affected = 0
        decoded = [(key, json.loads(payload)) for key, payload in tree.scan()]
        for key, row in decoded:
            binding = {table.name: tuple(row)}
            if stmt.where is not None and not _truthy(
                eval_expr(stmt.where, binding, resolver)
            ):
                continue
            tree.delete(key)
            affected += 1
        table.root = tree.root
        self._save_catalog()
        return Result(rows_affected=affected)

    # ------------------------------------------------------------------ SELECT

    def _exec_select(self, stmt: ast.Select) -> Result:
        if stmt.table.name == "":
            # FROM-less expression select: exactly one empty binding
            if stmt.joins:
                raise ExecutionError("JOIN requires a FROM clause")
            if any(isinstance(item.expr, ast.Func) for item in stmt.items):
                raise ExecutionError("aggregate functions require a FROM clause")
            resolver = _Resolver([])
            bindings: list[Binding] = [{}]
            tables: list[tuple[str, list[str]]] = []
        else:
            tables = [self._as_source(stmt.table)]
            joined_keys = {stmt.table.key}
            for join in stmt.joins:
                if join.table.key in joined_keys:
                    raise ExecutionError(f"duplicate table alias {join.table.key!r}")
                joined_keys.add(join.table.key)
                tables.append(self._as_source(join.table))
            resolver = _Resolver(tables)

            if stmt.where is not None and _contains_func(stmt.where):
                raise ExecutionError("aggregates are not allowed in WHERE")

            bindings = self._pk_lookup_bindings(stmt, resolver)
            if bindings is None:
                bindings = self._scan_base(stmt.table, resolver)
            for join in stmt.joins:
                bindings = self._apply_join(bindings, join, resolver)

        if stmt.where is not None:
            bindings = [
                b for b in bindings if _truthy(eval_expr(stmt.where, b, resolver))
            ]

        has_aggregate = any(isinstance(item.expr, ast.Func) for item in stmt.items)
        if has_aggregate or stmt.group_by:
            columns, out_rows = self._project_aggregate(stmt, bindings, resolver)
            order_envs = [
                {"out": dict(zip(columns, row, strict=True)), "binding": None}
                for row in out_rows
            ]
            order_by_output = True
        else:
            columns, projections = self._expand_items(stmt.items, tables, resolver)
            pairs: list[tuple[Binding, list[Value]]] = [
                (b, [eval_expr(e, b, resolver) for e in projections])
                for b in bindings
            ]
            out_rows = [vals for _b, vals in pairs]
            order_envs = [
                {"out": dict(zip(columns, vals, strict=True)), "binding": b}
                for b, vals in pairs
            ]
            order_by_output = False

        if stmt.distinct:
            seen: set[tuple] = set()
            kept_rows: list[list[Value]] = []
            kept_envs: list[dict] = []
            for row, env in zip(out_rows, order_envs, strict=True):
                marker = tuple(_sort_key(v) for v in row)
                if marker not in seen:
                    seen.add(marker)
                    kept_rows.append(row)
                    kept_envs.append(env)
            out_rows, order_envs = kept_rows, kept_envs

        if stmt.order_by:
            order_vals = [
                [self._order_value(item.expr, env, columns, resolver, order_by_output)
                 for item in stmt.order_by]
                for env in order_envs
            ]
            # classic stable-sort trick: sort by the last key first; each pass
            # only reorders ties left by later keys
            indices = list(range(len(out_rows)))
            for pos in range(len(stmt.order_by) - 1, -1, -1):
                desc = stmt.order_by[pos].desc
                indices.sort(
                    key=lambda i, p=pos: _sort_key(order_vals[i][p]),
                    reverse=desc,
                )
            out_rows = [out_rows[i] for i in indices]

        rows = out_rows
        if stmt.offset:
            rows = rows[stmt.offset:]
        if stmt.limit is not None:
            rows = rows[:stmt.limit]
        return Result(columns=columns, rows=[list(r) for r in rows])

    # SELECT helpers -----------------------------------------------------------

    def _as_source(self, ref: ast.TableRef) -> tuple[str, list[str]]:
        table = self._require_table(ref.name)
        return ref.key, table.columns

    def _scan_base(self, ref: ast.TableRef, resolver: _Resolver) -> list[Binding]:
        table = self._require_table(ref.name)
        tree = self._tree(table)
        rows = [json.loads(payload) for _key, payload in tree.scan()]
        return [{ref.key: tuple(r)} for r in rows]

    def _pk_lookup_bindings(self, stmt: ast.Select,
                            resolver: _Resolver) -> list[Binding] | None:
        """Seek the primary-key B-tree when WHERE pins it to one literal.

        Returns the matching bindings, ``[]`` when the key is absent, or
        ``None`` when the query does not qualify for the fast path. The full
        WHERE clause is still applied afterwards, so extra predicates stay
        correct.
        """
        if stmt.joins or stmt.where is None:
            return None
        eq = stmt.where
        if not (isinstance(eq, ast.Binary) and eq.op == "="):
            return None
        col = _as_column(eq.left)
        lit = eq.right
        if col is None:
            col, lit = _as_column(eq.right), eq.left
        if col is None or not isinstance(lit, ast.Literal):
            return None
        table = self._require_table(stmt.table.name)
        if not table.pk_is_stored_key:
            return None
        try:
            tkey, idx = resolver.resolve(col)
        except ExecutionError:
            return None
        if tkey != stmt.table.key or idx != table.pk_index:
            return None
        key = lit.value
        if not isinstance(key, int) or isinstance(key, bool):
            return None
        payload = self._tree(table).get(key)
        if payload is None:
            return []
        return [{stmt.table.key: tuple(json.loads(payload))}]

    def _apply_join(self, left: list[Binding], join: ast.JoinClause,
                    resolver: _Resolver) -> list[Binding]:
        table = self._require_table(join.table.name)
        tree = self._tree(table)
        right_rows = [json.loads(p) for _k, p in tree.scan()]
        tkey = join.table.key

        if isinstance(join.on, ast.Binary) and join.on.op == "=":
            lcol = _as_column(join.on.left)
            rcol = _as_column(join.on.right)
            if lcol is not None and rcol is not None:
                lt, li = resolver.resolve(lcol)
                rt, ri = resolver.resolve(rcol)
                known = set(left[0]) if left else set()
                # hash join applies when one side is the new table and the
                # other side lives in the already-bound tables
                if lt == tkey and rt in known:
                    lt, li, rt, ri = rt, ri, lt, li
                if lt in known and rt == tkey:
                    index: dict[Value, list[tuple]] = {}
                    for row in right_rows:
                        index.setdefault(row[ri], []).append(row)
                    out = []
                    for b in left:
                        probe = b[lt][li]
                        for row in index.get(probe, ()):
                            out.append({**b, tkey: tuple(row)})
                    return out
        # generic nested-loop join
        out = []
        for b in left:
            for row in right_rows:
                merged = {**b, tkey: tuple(row)}
                if _truthy(eval_expr(join.on, merged, resolver)):
                    out.append(merged)
        return out

    def _expand_items(self, items: list[ast.SelectItem],
                      tables: list[tuple[str, list[str]]],
                      resolver: _Resolver) -> tuple[list[str], list[ast.Expr]]:
        columns: list[str] = []
        projections: list[ast.Expr] = []
        for item in items:
            if isinstance(item.expr, ast.Star):
                for tkey, colnames in tables:
                    for col in colnames:
                        columns.append(col)
                        projections.append(ast.Column(tkey, col))
                continue
            if _contains_func(item.expr):
                raise ExecutionError("aggregates need GROUP BY or must fill the select list")
            columns.append(item.alias or expr_label(item.expr))
            projections.append(item.expr)
        return columns, projections

    def _project_aggregate(self, stmt: ast.Select, bindings: list[Binding],
                           resolver: _Resolver) -> tuple[list[str], list[list[Value]]]:
        for item in stmt.items:
            if isinstance(item.expr, ast.Star):
                raise ExecutionError("SELECT * cannot be mixed with aggregates")
            if isinstance(item.expr, ast.Func):
                if isinstance(item.expr.arg, ast.Expr) and _contains_func(item.expr.arg):
                    raise ExecutionError("nested aggregates are not supported")
            else:
                if not stmt.group_by:
                    pass  # whole-table group: bare columns are allowed
        for g in stmt.group_by:
            if _contains_func(g):
                raise ExecutionError("aggregates are not allowed in GROUP BY")

        groups: dict[tuple, list[Binding]] = {}
        if stmt.group_by:
            for b in bindings:
                key = tuple(_sort_key(eval_expr(g, b, resolver)) for g in stmt.group_by)
                groups.setdefault(key, []).append(b)
        else:
            groups[()] = bindings

        group_exprs = stmt.group_by
        columns: list[str] = []
        plan: list[tuple[str, Any]] = []  # ("group", index) | ("agg", Func)
        for item in stmt.items:
            label = item.alias or expr_label(item.expr)
            if isinstance(item.expr, ast.Func):
                columns.append(label)
                plan.append(("agg", item.expr))
            else:
                match = next(
                    (i for i, g in enumerate(group_exprs) if _same_expr(item.expr, g)),
                    None,
                )
                if match is None and _column_refs(item.expr):
                    raise ExecutionError(
                        f"column expression {label!r} must appear in GROUP BY"
                    )
                columns.append(label)
                plan.append(("group", item.expr, match))

        rows: list[list[Value]] = []
        for group_bindings in groups.values():
            row: list[Value] = []
            for entry in plan:
                if entry[0] == "agg":
                    row.append(_aggregate_value(entry[1], group_bindings, resolver))
                else:
                    sample = group_bindings[0] if group_bindings else {}
                    row.append(eval_expr(entry[1], sample, resolver))
            rows.append(row)
        return columns, rows

    def _order_value(self, expr: ast.Expr, env: dict, columns: list[str],
                     resolver: _Resolver, order_by_output: bool) -> Value:
        out: dict[str, Value] = env["out"]
        if isinstance(expr, ast.Literal) and type(expr.value) is int and \
                1 <= expr.value <= len(columns):
            return out[columns[expr.value - 1]]
        if isinstance(expr, ast.Column) and expr.table is None and expr.name in out:
            return out[expr.name]
        if order_by_output:
            raise ExecutionError(
                "ORDER BY on aggregate queries must reference output columns"
            )
        return eval_expr(expr, env["binding"], resolver)


def _as_column(expr: ast.Expr) -> ast.Column | None:
    return expr if isinstance(expr, ast.Column) else None
