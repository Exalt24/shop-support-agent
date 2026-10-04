"""Customer memory through the HTTP API: the chat turn writes it, staff can read and erase it, and one store's notes
stay out of another store's reach."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from agent.core import Result
from agent.memory import Candidate
from console_api import auth
from console_api.main import app

H = {"X-Requested-With": "console"}
PW = "t-" + uuid.uuid4().hex  # generated per run: no password literal sits in the source
CUSTOMER = "miguel@example.com"


class MemoryStubAgent:
    """Stands in for the engine: when the console attaches memory it writes through it, as the real agent does."""

    def __init__(self):
        self.memory, self.seen_notes = None, []

    def attach_memory(self, store):
        self.memory = store

    def handle(self, message, email):
        self.seen_notes = self.memory.recall(email) if self.memory else []
        if self.memory:
            self.memory.remember(email, [Candidate("preference", "contact_channel", "email me, not sms"),
                                         Candidate("live_ref", "last_order", None, "agent_note", ref="shopify:order:#1019")])
        return Result(action="replied", reply="Your order #1019 is paid and has not shipped yet.", intent="order_status",
                      order_name="#1019", steps=["triage: intent=order_status"])


@pytest.fixture
def api(admin, world):
    for uid, pw in ((world.admin_a, PW), (world.agent_a, PW), (world.agent_b, PW)):
        admin.execute("update users set password_hash = %s where id = %s", (auth.hash_password(pw), uid))
    stub = MemoryStubAgent()
    app.state.agent_factory = lambda tenant: stub
    auth._fails.clear()
    with TestClient(app):
        world.stub = stub
        yield world


def client_for(email, password):
    c = TestClient(app, headers=H)
    r = c.post("/api/auth/login", json=dict(email=email, password=password))
    assert r.status_code == 200, r.text
    return c


def chat_once(c, store="store-a"):
    r = c.post("/api/t/%s/chat" % store, json={"email": CUSTOMER, "message": "Where is order 1019? Email me, not sms."})
    assert r.status_code == 200, r.text
    return r.json()


def notes(c, store="store-a", email=CUSTOMER):
    r = c.get("/api/t/%s/customers/%s/memory" % (store, email))
    assert r.status_code == 200, r.text
    return r.json()["items"]


def test_a_chat_turn_attaches_memory_for_that_store_and_the_notes_are_visible_to_staff(api):
    a = client_for("agent-a@x.com", PW)
    chat_once(a)
    assert api.stub.memory is not None
    got = {n["key"]: n for n in notes(a)}
    assert set(got) == {"contact_channel", "last_order"}
    assert got["contact_channel"]["body"] == "email me, not sms" and got["contact_channel"]["age_days"] == 0
    assert got["last_order"]["body"] is None and got["last_order"]["ref"] == "shopify:order:#1019"
    assert "pointer only" in got["last_order"]["use"] and "hint" in got["contact_channel"]["use"]


def test_the_next_turn_recalls_what_the_last_one_wrote(api):
    a = client_for("agent-a@x.com", PW)
    chat_once(a)
    chat_once(a)
    assert {n.key for n in api.stub.seen_notes} == {"contact_channel", "last_order"}


def test_the_other_store_sees_nothing_for_the_same_customer_and_cannot_erase_it(api, admin):
    a, b = client_for("agent-a@x.com", PW), client_for("agent-b@x.com", PW)
    chat_once(a)
    mem_id = notes(a)[0]["id"]
    assert notes(b, "store-b") == []
    assert b.get("/api/t/store-a/customers/%s/memory" % CUSTOMER).status_code == 404  # not a member: same answer as no such store
    r = b.delete("/api/t/store-b/memory/%s" % mem_id)
    assert r.status_code == 404
    assert admin.execute("select count(*) from customer_memories where id = %s", (mem_id,)).fetchone()[0] == 1


def test_a_stale_note_is_flagged_for_staff(api, admin):
    admin.execute("insert into customer_memories (tenant_id, customer_email, kind, key, body, source, observed_at) "
                  "values (%s, %s, 'fact', 'dog_name', 'dog is called Biscuit', 'customer_said', %s)",
                  (api.ta, CUSTOMER, datetime.now(timezone.utc) - timedelta(days=100)))
    got = notes(client_for("agent-a@x.com", PW))
    assert got[0]["stale"] is True and got[0]["age_days"] >= 100 and got[0]["use"].startswith("stale")


def test_erasing_one_note_removes_it_and_leaves_the_rest_and_is_audited_without_the_words(api, admin):
    a = client_for("agent-a@x.com", PW)
    chat_once(a)
    target = next(n for n in notes(a) if n["key"] == "contact_channel")
    assert a.delete("/api/t/store-a/memory/%s" % target["id"]).status_code == 200
    assert {n["key"] for n in notes(a)} == {"last_order"}
    assert a.delete("/api/t/store-a/memory/%s" % target["id"]).status_code == 404  # already gone
    adm = client_for("admin-a@x.com", PW)
    log = adm.get("/api/t/store-a/audit?action=memory.forget").json()["items"]
    assert [i["detail"] for i in log] == [{"scope": "one", "notes": 1}] and log[0]["actor"] == "agent-a@x.com"


def test_erasing_a_customer_removes_their_notes_in_this_store_only(api, admin):
    a, b = client_for("agent-a@x.com", PW), client_for("agent-b@x.com", PW)
    chat_once(a)
    chat_once(b, "store-b")
    r = a.delete("/api/t/store-a/customers/%s/memory" % CUSTOMER.upper())
    assert r.status_code == 200 and r.json() == {"forgotten": 2}
    assert notes(a) == [] and len(notes(b, "store-b")) == 2


def test_viewing_notes_is_audited_under_the_staff_members_own_name(api):
    a = client_for("agent-a@x.com", PW)
    chat_once(a)
    notes(a)
    adm = client_for("admin-a@x.com", PW)
    views = adm.get("/api/t/store-a/audit?action=memory.view").json()["items"]
    assert views and views[0]["actor"] == "agent-a@x.com" and views[0]["detail"] == {"notes": 2}


def test_bad_input_is_a_clean_error_and_nothing_is_reachable_without_a_session(api):
    a = client_for("agent-a@x.com", PW)
    assert a.get("/api/t/store-a/customers/not-an-email/memory").status_code == 422
    assert a.delete("/api/t/store-a/memory/not-a-uuid").status_code == 404
    anon = TestClient(app, headers=H)
    assert anon.get("/api/t/store-a/customers/%s/memory" % CUSTOMER).status_code == 401
    no_header = TestClient(app)
    no_header.cookies.update(a.cookies)
    assert no_header.delete("/api/t/store-a/customers/%s/memory" % CUSTOMER).status_code == 403  # the CSRF guard still covers it
