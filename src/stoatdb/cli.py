"""Command-line interface.

``stoatdb <file.db>`` opens an interactive REPL; ``stoatdb <file.db> -c "SQL"``
runs one or more statements and prints the result. Output is rendered as an
ASCII table. Errors go to stderr with exit code 1.
"""

from __future__ import annotations

import argparse
import sys

from .engine import Database
from .errors import StoatError
from .sql.lexer import split_statements


def render_table(columns: list[str], rows: list[list[object]]) -> str:
    if not columns:
        return ""
    cells = [list(map(str, row)) for row in rows]
    widths = [
        max(len(columns[i]), *(len(row[i]) for row in cells)) if cells else len(columns[i])
        for i in range(len(columns))
    ]
    top = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    head = "|" + "|".join(f" {c.ljust(w)} " for c, w in zip(columns, widths, strict=True)) + "|"
    lines = [top, head, top]
    for row in cells:
        lines.append(
            "|"
            + "|".join(
                f" {v.ljust(w)} " for v, w in zip(row, widths, strict=False)
            )
            + "|"
        )
    lines.append(top)
    lines.append(f"{len(rows)} row(s)")
    return "\n".join(lines)


def run_script(db: Database, sql: str, out=sys.stdout) -> None:
    for statement in split_statements(sql):
        result = db.execute(statement)
        if result.columns:
            out.write(render_table(result.columns, result.rows) + "\n")
        elif result.rows_affected:
            out.write(f"{result.rows_affected} row(s) affected\n")
        else:
            out.write("ok\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="stoatdb", description="stoatdb — a tiny relational database engine"
    )
    parser.add_argument("file", help="database file (created if missing)")
    parser.add_argument("-c", "--command", help="run SQL and exit")
    args = parser.parse_args(argv)

    try:
        db = Database(args.file)
    except (ValueError, OSError) as exc:
        print(f"stoatdb: {exc}", file=sys.stderr)
        return 2

    if args.command is not None:
        code = 0
        try:
            run_script(db, args.command)
        except StoatError as exc:
            print(f"stoatdb: {exc}", file=sys.stderr)
            code = 1
        finally:
            db.close()
        return code

    return repl(db)


def repl(db: Database) -> int:
    print("stoatdb " + __import__("stoatdb").__version__ +
          " — type .help for help")
    buffer: list[str] = []
    while True:
        prompt = "stoat> " if not buffer else "  ...> "
        try:
            line = input(prompt)
        except (EOFError, KeyboardInterrupt):
            print()
            db.close()
            return 0
        stripped = line.strip()
        if not buffer and stripped.startswith("."):
            if stripped in (".quit", ".exit"):
                db.close()
                return 0
            if stripped == ".help":
                print(".quit  exit\n.help  this text")
                continue
            if stripped == ".tables":
                names = db.table_names()
                print("\n".join(names) if names else "(no tables)")
                continue
            if stripped.startswith(".schema "):
                name = stripped.split(None, 1)[1].strip()
                try:
                    print(db.schema_sql(name))
                except StoatError as exc:
                    print(f"error: {exc}", file=sys.stderr)
                continue
            print(f"unknown command: {stripped}", file=sys.stderr)
            continue
        buffer.append(line)
        if buffer[-1].rstrip().endswith(";"):
            sql = "\n".join(buffer)
            buffer = []
            try:
                run_script(db, sql)
            except StoatError as exc:
                print(f"error: {exc}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
