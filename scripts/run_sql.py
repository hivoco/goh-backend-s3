"""Apply a .sql file using the app's own database connection.

Why this exists: the Homebrew `mysql` client (9.x) dropped the
`mysql_native_password` plugin, so `mysql -h … < file.sql` fails on macOS with

    ERROR 2059 (HY000): Authentication plugin 'mysql_native_password'
    cannot be loaded

against an RDS user created with that plugin. PyMySQL — which the API already
uses — handles it fine, so this runs the same file through the same driver.

    cd backend
    PYTHONPATH=. .venv/bin/python scripts/run_sql.py migrations/001_share_and_admin_tables.sql
    PYTHONPATH=. .venv/bin/python scripts/run_sql.py --dry-run sql/seed_vision_gemini.sql

Statements are executed one at a time so a failure names the statement that
broke rather than aborting an opaque batch.
"""

import argparse
import os
import pathlib
import sys

BACKEND_DIR = pathlib.Path(__file__).resolve().parent.parent
ENV_PATH = BACKEND_DIR / ".env"


def load_env() -> None:
    if not ENV_PATH.is_file():
        print(f"✗ No .env at {ENV_PATH} — copy .env.example and fill in DATABASE_URL.")
        raise SystemExit(1)
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def split_statements(sql: str) -> list[str]:
    """Split on semicolons that are OUTSIDE quoted strings and comments.

    A naive `sql.split(";")` corrupts any statement containing a semicolon in a
    string literal — the vision-config seed's prompt has two.
    """
    out, buf = [], []
    quote = None          # "'" or '"' while inside a literal
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        if quote:
            buf.append(ch)
            if ch == quote:
                if nxt == quote:          # '' escape inside a literal
                    buf.append(nxt); i += 2; continue
                quote = None
            elif ch == "\\":              # backslash escape
                buf.append(nxt); i += 2; continue
            i += 1
            continue

        if ch in ("'", '"'):
            quote = ch; buf.append(ch); i += 1; continue
        if ch == "-" and nxt == "-":      # line comment
            while i < n and sql[i] != "\n":
                i += 1
            continue
        if ch == "/" and nxt == "*":      # block comment
            i = sql.find("*/", i)
            i = n if i == -1 else i + 2
            continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []; i += 1; continue

        buf.append(ch); i += 1

    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Run a .sql file through the app's DB connection.")
    ap.add_argument("file", help="path to the .sql file")
    ap.add_argument("--dry-run", action="store_true",
                    help="list the statements without executing anything")
    args = ap.parse_args()

    path = pathlib.Path(args.file)
    if not path.is_file():
        path = BACKEND_DIR / args.file
    if not path.is_file():
        print(f"✗ No such file: {args.file}")
        return 1

    load_env()
    from sqlalchemy import text
    from app.core.database import engine

    statements = split_statements(path.read_text())
    print(f"{path.name}: {len(statements)} statement(s)\n")

    if args.dry_run:
        for i, s in enumerate(statements, 1):
            print(f"  [{i}] {' '.join(s.split())[:110]}…")
        print("\n(dry run — nothing executed)")
        return 0

    with engine.begin() as conn:
        for i, stmt in enumerate(statements, 1):
            label = " ".join(stmt.split())[:80]
            try:
                conn.execute(text(stmt))
                print(f"  [{i}/{len(statements)}] ok   {label}")
            except Exception as e:
                print(f"  [{i}/{len(statements)}] FAIL {label}\n        {e}")
                return 1

    print("\n✓ applied. Verify with:  PYTHONPATH=. .venv/bin/python inspect_schema.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
