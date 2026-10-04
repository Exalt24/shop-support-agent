"""Break each database guard on purpose and confirm the console suite goes red.

    python tests_console/mutation_check.py

A passing test suite only means something if it fails when the protection is gone. Each mutant rewrites one rule in
sql/schema.sql, rebuilds the local database from the mutated file, and runs tests_console. A mutant the suite does not
catch is a hole in the tests, and the run exits 1. The real schema is restored at the end, whatever happened.
Needs the local Postgres from README.md. It rewrites the `console` database, so never point it at real data.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from console_api.migrate import ADMIN_DSN, APP_PASSWORD, SCHEMA  # noqa: E402

REAL = SCHEMA.read_text(encoding="utf-8")

MUTANTS = [
    ("conversations readable across tenants", "create policy conversations_tenant on conversations for all\n  using (tenant_id = app_tenant())",
     "create policy conversations_tenant on conversations for all\n  using (true)"),
    ("messages readable across tenants", "create policy messages_tenant on messages for all\n  using (tenant_id = app_tenant())",
     "create policy messages_tenant on messages for all\n  using (true)"),
    ("handoffs readable across tenants", "create policy handoffs_tenant on handoffs for all\n  using (tenant_id = app_tenant())",
     "create policy handoffs_tenant on handoffs for all\n  using (true)"),
    ("conversation writes accepted for another tenant", "create policy conversations_tenant on conversations for all\n  using (tenant_id = app_tenant()) with check (tenant_id = app_tenant())",
     "create policy conversations_tenant on conversations for all\n  using (tenant_id = app_tenant()) with check (true)"),
    ("agents can read the audit log", "using (tenant_id = app_tenant() and app_role() = 'admin');\ncreate policy audit_append",
     "using (tenant_id = app_tenant());\ncreate policy audit_append"),
    ("audit log editable and deletable", "grant select, insert on audit_log to console_app;",
     "grant select, insert, update, delete on audit_log to console_app;"),
    ("audit entries can be written for another tenant", "create policy audit_append on audit_log for insert with check (tenant_id = app_tenant());",
     "create policy audit_append on audit_log for insert with check (true);"),
    ("every membership visible", "using (user_id = app_user());", "using (true);"),
    ("every user row visible", "create policy users_self on users for select using (id = app_user());",
     "create policy users_self on users for select using (true);"),
    ("every store visible", "using (\n  id = app_tenant() or id in (select tenant_id from memberships where user_id = app_user()));",
     "using (true);"),
    ("agents can edit the policy", "using (id = app_tenant() and app_role() = 'admin')\n  with check (id = app_tenant() and app_role() = 'admin');",
     "using (id = app_tenant())\n  with check (id = app_tenant());"),
    ("app role bypasses row security", "reset role;\n", "reset role;\nalter role console_app bypassrls;\n"),
    ("app role may rewrite any tenant column, token included","grant select on tenants to console_app;",
     "grant select on tenants to console_app;\ngrant update on tenants to console_app;"),
    # ---- customer memory -------------------------------------------------------------------------------------
    ("customer notes readable across stores",
     "create policy customer_memories_tenant on customer_memories for all\n  using (tenant_id = app_tenant()) with check (tenant_id = app_tenant());",
     "create policy customer_memories_tenant on customer_memories for all\n  using (true) with check (tenant_id = app_tenant());"),
    ("customer notes writable for another store",
     "create policy customer_memories_tenant on customer_memories for all\n  using (tenant_id = app_tenant()) with check (tenant_id = app_tenant());",
     "create policy customer_memories_tenant on customer_memories for all\n  using (tenant_id = app_tenant()) with check (true);"),
    ("an unbound request reads every store's notes",
     "create policy customer_memories_tenant on customer_memories for all\n  using (tenant_id = app_tenant()) with check (tenant_id = app_tenant());",
     "create policy customer_memories_tenant on customer_memories for all\n  using (tenant_id = app_tenant() or app_tenant() is null) with check (tenant_id = app_tenant());"),
    ("a remembered sentence can be edited in place",
     "grant update (superseded_by) on customer_memories to console_app;",
     "grant update on customer_memories to console_app;"),
    ("a live pointer can carry a copy of the order",
     "constraint live_ref_is_a_pointer check (\n    (kind = 'live_ref' and body is null and ref is not null) or (kind <> 'live_ref' and body is not null and ref is null))",
     "constraint live_ref_is_a_pointer check (true)"),
    ("two current notes for one slot",
     "create unique index customer_memories_one_current on customer_memories (tenant_id, customer_email, kind, key)\n  where superseded_by is null;",
     "create index customer_memories_one_current on customer_memories (tenant_id, customer_email, kind, key)\n  where superseded_by is null;"),
    ("a customer email stored in any case",
     "customer_email text not null check (customer_email = lower(customer_email)),\n  kind text not null check (kind in ('preference'",
     "customer_email text not null,\n  kind text not null check (kind in ('preference'"),
    ("a note key can be any string",
     "key text not null check (key ~ '^[a-z][a-z0-9_]{1,40}$'),\n  body text check (body is null",
     "key text not null,\n  body text check (body is null"),
    ("a store cannot erase a customer's notes",
     "grant select, insert, delete on customer_memories to console_app;",
     "grant select, insert on customer_memories to console_app;"),
]


def rebuild(schema_text: str) -> None:
    with psycopg.connect(ADMIN_DSN, autocommit=True) as c:
        c.execute("alter role console_app nobypassrls")
        c.execute("drop schema public cascade")
        c.execute("create schema public")
        c.execute("grant usage on schema public to public")
        c.execute(schema_text)
        c.execute(sql.SQL("alter role console_app password {}").format(sql.Literal(APP_PASSWORD)))


def suite_passes() -> bool:
    r = subprocess.run([sys.executable, "-m", "pytest", "tests_console", "-q", "-x", "-p", "no:cacheprovider"],
                       cwd=ROOT, capture_output=True, text=True)
    return r.returncode == 0


def main() -> int:
    survived = []
    try:
        for name, old, new in MUTANTS:
            if old not in REAL:
                print(f"BAD MUTANT  {name}: the text it rewrites is not in schema.sql, fix the harness")
                return 2
            rebuild(REAL.replace(old, new, 1))
            caught = not suite_passes()
            print(f"{'caught  ' if caught else 'SURVIVED'}  {name}")
            if not caught:
                survived.append(name)
    finally:
        rebuild(REAL)
    clean = suite_passes()
    print(f"\nreal schema restored, suite {'green' if clean else 'RED (investigate)'}")
    print(f"{len(MUTANTS) - len(survived)} of {len(MUTANTS)} mutants caught")
    return 0 if (not survived and clean) else 1


if __name__ == "__main__":
    raise SystemExit(main())
