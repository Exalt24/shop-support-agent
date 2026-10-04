"""The database side of customer memory. Every statement runs inside db.tx(), so the tenant is bound for that
transaction only and row-level security scopes it. The agent never sees the database: it gets this object attached
for one turn and calls recall() and remember().

What is audited is identifiers and counts, never the remembered words, so the audit log is not a second copy of
customer data.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import psycopg
from psycopg.types.json import Jsonb

from agent.memory import Candidate, Memory, age_and_stale

from . import db
from .auth import Member

RECALL_LIMIT = 40
WRITE_RETRIES = 4


def _audit(c, tenant_id: str, actor: str, action: str, detail: dict) -> None:
    c.execute("insert into audit_log (tenant_id, actor, action, detail) values (%s, %s, %s, %s)",
              (tenant_id, actor, action, Jsonb(detail)))


def _to_memory(row: dict, now: datetime) -> Memory:
    days, stale = age_and_stale(row["kind"], row["observed_at"], now)
    return Memory(id=str(row["id"]), kind=row["kind"], key=row["key"], body=row["body"], ref=row["ref"], source=row["source"],
                  observed_at=row["observed_at"], age_days=days, stale=stale)


class PgMemory:
    def __init__(self, member: Member, conversation_id: str | None = None, clock=None, actor: str = "agent"):
        self._m, self._conversation_id, self._actor = member, conversation_id, actor
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _tx(self):
        return db.tx(tenant_id=self._m.tenant_id, user_id=self._m.user_id, role=self._m.role)

    def recall(self, email: str) -> list[Memory]:
        email, now = email.strip().lower(), self._clock()
        with self._tx() as c:
            rows = c.execute(
                "select id, kind, key, body, ref, source, observed_at from customer_memories "
                "where tenant_id = %s and customer_email = %s and superseded_by is null "
                "order by observed_at desc limit %s", (self._m.tenant_id, email, RECALL_LIMIT)).fetchall()
            _audit(c, self._m.tenant_id, self._actor, "memory.recall" if self._actor == "agent" else "memory.view",
                   {"notes": len(rows)})
        return [_to_memory(r, now) for r in rows]

    def remember(self, email: str, items: list[Candidate]) -> int:
        email, wrote, kinds = email.strip().lower(), 0, []
        for cand in items:
            for attempt in range(WRITE_RETRIES):
                try:
                    with self._tx() as c:
                        wrote += self._write_one(c, email, cand)
                    kinds.append(cand.kind)
                    break
                except psycopg.errors.UniqueViolation:
                    # two writers raced for the same slot: the loser retries and supersedes the winner
                    if attempt == WRITE_RETRIES - 1:
                        raise
        if wrote:
            with self._tx() as c:
                _audit(c, self._m.tenant_id, "agent", "memory.write", {"notes": wrote, "kinds": sorted(set(kinds))})
        return wrote

    def _write_one(self, c, email: str, cand: Candidate) -> int:
        # Writers for one slot queue on a transaction-scoped advisory lock instead of racing: under contention a loser
        # could lose repeatedly and exhaust its retries. The unique index stays as the backstop that makes a second
        # current row impossible whatever the application does.
        c.execute("select pg_advisory_xact_lock(hashtextextended(%s, 0))",
                  ("%s|%s|%s|%s" % (self._m.tenant_id, email, cand.kind, cand.key),))
        current = c.execute(
            "select id, observed_at from customer_memories where tenant_id = %s and customer_email = %s and kind = %s "
            "and key = %s and superseded_by is null", (self._m.tenant_id, email, cand.kind, cand.key)).fetchone()
        new_id, observed = str(uuid.uuid4()), cand.observed_at or self._clock()
        if current and current["observed_at"] > observed:
            superseded_by = str(current["id"])  # arrived late and older: kept as history, never wins
        else:
            superseded_by = None
            if current:
                c.execute("update customer_memories set superseded_by = %s where id = %s", (new_id, current["id"]))
        c.execute(
            "insert into customer_memories (id, tenant_id, customer_email, kind, key, body, ref, source, conversation_id, "
            "observed_at, superseded_by) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (new_id, self._m.tenant_id, email, cand.kind, cand.key, cand.body, cand.ref, cand.source,
             self._conversation_id, observed, superseded_by))
        return 1


def list_for_staff(m: Member, email: str, actor: str, now: datetime | None = None) -> list[Memory]:
    return PgMemory(m, clock=(lambda: now) if now else None, actor=actor).recall(email)


def forget_one(m: Member, memory_id: str) -> int:
    with db.tx(tenant_id=m.tenant_id, user_id=m.user_id, role=m.role) as c:
        return c.execute("delete from customer_memories where id = %s and tenant_id = %s", (memory_id, m.tenant_id)).rowcount


def forget_customer(m: Member, email: str) -> int:
    with db.tx(tenant_id=m.tenant_id, user_id=m.user_id, role=m.role) as c:
        return c.execute("delete from customer_memories where customer_email = %s and tenant_id = %s",
                         (email.strip().lower(), m.tenant_id)).rowcount
