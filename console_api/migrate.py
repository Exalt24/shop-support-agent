"""Apply sql/schema.sql as a superuser, then set the app role's password. `--reset` drops everything first.

    python -m console_api.migrate --reset

The app never runs this and never holds the admin DSN: it connects as console_app only.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

import psycopg
from psycopg import sql

ADMIN_DSN = os.environ.get("CONSOLE_ADMIN_DSN", "postgresql://postgres:localsuper@127.0.0.1:54931/console")
APP_PASSWORD = os.environ.get("CONSOLE_APP_PASSWORD", "localapp-demo")
SCHEMA = Path(__file__).resolve().parent.parent / "sql" / "schema.sql"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="drop and recreate the public schema first")
    args = ap.parse_args()
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        if args.reset:
            conn.execute("drop schema public cascade")
            conn.execute("create schema public")
            conn.execute("grant usage on schema public to public")
        conn.execute(SCHEMA.read_text(encoding="utf-8"))
        conn.execute(sql.SQL("alter role console_app password {}").format(sql.Literal(APP_PASSWORD)))
        flags = conn.execute(
            "select rolsuper, rolbypassrls from pg_roles where rolname = 'console_app'").fetchone()
        assert flags == (False, False), "console_app must be neither superuser nor BYPASSRLS, got %r" % (flags,)
        print("schema applied; console_app is not a superuser and does not bypass RLS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
