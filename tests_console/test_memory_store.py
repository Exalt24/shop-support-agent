"""PgMemory: the store the agent is handed for one turn. Real Postgres, real app role."""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from agent.memory import Candidate
from console_api import db
from console_api.auth import Member
from console_api.memory_store import PgMemory, forget_customer, forget_one

EMAIL = "customer@x.com"


def member(world, which="a"):
    if which == "a":
        return Member(str(world.agent_a), str(world.ta), "store-a", "Store A", "agent")
    return Member(str(world.agent_b), str(world.tb), "store-b", "Store B", "agent")


def pref(body="email me, not sms", key="contact_channel", **kw):
    return Candidate("preference", key, body, **kw)


def test_notes_round_trip_and_a_pointer_comes_back_with_no_body(world):
    m = PgMemory(member(world))
    assert m.remember(EMAIL, [pref(), Candidate("episode", "last_order_status", "asked where order #1019 was", "agent_note"),
                              Candidate("live_ref", "last_order", None, "agent_note", ref="shopify:order:#1019")]) == 3
    got = {n.key: n for n in m.recall(EMAIL)}
    assert set(got) == {"contact_channel", "last_order_status", "last_order"}
    assert got["last_order"].body is None and got["last_order"].ref == "shopify:order:#1019"
    assert got["contact_channel"].age_days == 0 and not got["contact_channel"].stale


def test_a_newer_observation_supersedes_and_recall_returns_only_the_current_one(world, admin):
    m = PgMemory(member(world))
    m.remember(EMAIL, [pref("email me, not sms")])
    m.remember(EMAIL, [pref("whatsapp is best")])
    assert [n.body for n in m.recall(EMAIL)] == ["whatsapp is best"]
    assert admin.execute("select count(*) from customer_memories where tenant_id = %s", (world.ta,)).fetchone()[0] == 2  # history kept


def test_a_late_arriving_older_note_is_kept_as_history_and_never_wins(world, admin):
    m = PgMemory(member(world))
    m.remember(EMAIL, [pref("whatsapp is best")])
    m.remember(EMAIL, [pref("email me, not sms", observed_at=datetime.now(timezone.utc) - timedelta(days=3))])
    assert [n.body for n in m.recall(EMAIL)] == ["whatsapp is best"]
    rows = admin.execute("select body, superseded_by is not null from customer_memories order by created_at").fetchall()
    assert rows == [("whatsapp is best", False), ("email me, not sms", True)]


def test_staleness_comes_from_the_clock_and_the_kind(world):
    PgMemory(member(world)).remember(EMAIL, [Candidate("fact", "dog_name", "dog is called Biscuit"), pref()])
    later = PgMemory(member(world), clock=lambda: datetime.now(timezone.utc) + timedelta(days=100))
    got = {n.key: n for n in later.recall(EMAIL)}
    assert got["dog_name"].stale and got["dog_name"].age_days >= 100  # a fact goes stale after 60 days
    assert not got["contact_channel"].stale  # a preference after 180


def test_two_stores_never_see_or_overwrite_each_others_notes_for_one_customer(world):
    a, b = PgMemory(member(world, "a")), PgMemory(member(world, "b"))
    a.remember(EMAIL, [pref("email only")])
    assert b.recall(EMAIL) == []
    b.remember(EMAIL, [pref("sms only")])
    assert [n.body for n in a.recall(EMAIL)] == ["email only"]  # B's write did not supersede A's
    assert [n.body for n in b.recall(EMAIL)] == ["sms only"]


def test_the_customer_email_is_normalised_so_case_never_splits_a_customer(world):
    m = PgMemory(member(world))
    m.remember("  Customer@X.com ", [pref()])
    assert len(m.recall("CUSTOMER@x.com")) == 1


def test_racing_writers_leave_exactly_one_current_note_and_nobody_errors(world, admin):
    m = member(world)
    db.pool()  # the app opens the pool once at startup (lifespan); eight threads must not race to create it

    def write(i):
        PgMemory(m).remember(EMAIL, [pref("preference number %d" % i)])
        return i

    with ThreadPoolExecutor(max_workers=8) as ex:
        assert sorted(ex.map(write, range(8))) == list(range(8))
    current = admin.execute("select count(*) from customer_memories where tenant_id = %s and superseded_by is null", (world.ta,)).fetchone()[0]
    total = admin.execute("select count(*) from customer_memories where tenant_id = %s", (world.ta,)).fetchone()[0]
    assert current == 1 and total == 8


def test_the_audit_trail_records_counts_and_kinds_never_the_remembered_words(world, admin):
    m = PgMemory(member(world))
    m.remember(EMAIL, [pref("a very private sentence about the customer")])
    m.recall(EMAIL)
    rows = admin.execute("select actor, action, detail::text from audit_log where action like 'memory.%%' order by id").fetchall()
    assert [(r[0], r[1]) for r in rows] == [("agent", "memory.write"), ("agent", "memory.recall")]
    assert all("private" not in r[2] and EMAIL not in r[2] for r in rows)
    assert json.loads(rows[0][2]) == {"notes": 1, "kinds": ["preference"]}


def test_forgetting_one_note_or_a_whole_customer_only_touches_the_callers_store(world, admin):
    a, b = PgMemory(member(world, "a")), PgMemory(member(world, "b"))
    a.remember(EMAIL, [pref(), Candidate("fact", "dog_name", "dog is called Biscuit")])
    b.remember(EMAIL, [pref("sms only")])
    a_id = a.recall(EMAIL)[0].id
    b_id = b.recall(EMAIL)[0].id
    assert forget_one(member(world, "a"), b_id) == 0 and forget_one(member(world, "a"), a_id) == 1  # control: own note works
    assert forget_customer(member(world, "a"), EMAIL.upper()) == 1
    assert [n.body for n in b.recall(EMAIL)] == ["sms only"]
