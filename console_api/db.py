"""Database access. Every tenant-scoped statement runs inside tx(), which binds the tenant, the user and
the role for THIS transaction only (set_config(..., true)). Nothing is bound at session level, so a pooled
connection can never hand one request's tenant to the next.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

APP_DSN = os.environ.get("CONSOLE_APP_DSN", "postgresql://console_app:localapp-demo@127.0.0.1:54931/console")

_pool: ConnectionPool | None = None


class UnsafeDatabaseRole(RuntimeError):
    pass


def assert_role_is_safe(conn: psycopg.Connection) -> None:
    """Refuse to serve if RLS would be bypassed. A superuser, a BYPASSRLS role or a table owner reads every
    tenant's rows whatever the policies say, and nothing in the catalogue looks wrong in that state."""
    row = conn.execute(
        "select r.rolsuper, r.rolbypassrls, "
        "       exists (select 1 from pg_class c where c.relname = 'conversations' and c.relowner = r.oid) as owns "
        "from pg_roles r where r.rolname = current_user").fetchone()
    if not row:
        raise UnsafeDatabaseRole("cannot read the current role")
    if row["rolsuper"] or row["rolbypassrls"] or row["owns"]:
        raise UnsafeDatabaseRole(
            "the API role must be neither superuser, BYPASSRLS nor the table owner: row-level security "
            "would not apply to it (super=%s bypass=%s owner=%s)" % (row["rolsuper"], row["rolbypassrls"], row["owns"]))


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        p = ConnectionPool(APP_DSN, min_size=1, max_size=int(os.environ.get("CONSOLE_POOL_MAX", "8")),
                           kwargs={"row_factory": dict_row}, open=False)
        p.open(wait=True, timeout=15)
        with p.connection() as conn:
            assert_role_is_safe(conn)
        _pool = p
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def tx(tenant_id: str | None = None, user_id: str | None = None, role: str | None = None) -> Iterator[psycopg.Connection]:
    with pool().connection() as conn:
        with conn.transaction():
            conn.execute(
                "select set_config('app.tenant_id', %s, true), set_config('app.user_id', %s, true), "
                "set_config('app.role', %s, true)", (tenant_id or "", user_id or "", role or ""))
            yield conn
