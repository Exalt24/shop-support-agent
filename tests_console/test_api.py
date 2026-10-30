"""API behaviour with a stub engine and a stub store (the engine itself has its own 49 tests and 11 mutants)."""
import pytest
from fastapi.testclient import TestClient

from agent.core import Result
from agent.shopify import OrderFacts, ProductFacts, ShopifyError, StaffOrderView
from console_api import auth, db
from console_api.main import app
from console_api.secrets_store import encrypt_token

H = {"X-Requested-With": "console"}


class StubAgent:
    def __init__(self):
        self.calls = []

    def handle(self, message, email):
        self.calls.append((message, email))
        low = message.lower()
        if "sue" in low or "lawyer" in low:
            return Result(action="escalated", reply="A teammate will reply.", intent="other",
                          escalation_reason="legal", steps=["hard escalation rule matched before triage", "escalated: legal"])
        return Result(action="replied", reply="Your order #1019 is paid and has not shipped yet.", intent="order_status",
                      order_name="#1019", steps=["triage: intent=order_status", "order lookup: found #1019", "guard: passed on attempt 1"])


class StubShop:
    def find_order_for_staff(self, name):
        if name.lstrip("#") == "1019":
            return StaffOrderView(email="miguel@example.com", facts=OrderFacts(
                name="#1019", created_at="2026-09-20T10:00:00Z", financial_status="paid", fulfillment_status="unfulfilled",
                total="250.0", currency="USD", items=("2x USB Audio Interface",), tracking=()))
        if name.lstrip("#") == "5000":
            raise ShopifyError("outage")
        return None

    def search_products(self, text, limit=3):
        return [ProductFacts("65% Mechanical Keyboard", "149.0", "USD", True, "Hot-swappable.")]


@pytest.fixture
def api(admin, world):
    # real passwords for the seeded users
    for uid, pw in ((world.admin_a, "pw-admin-a"), (world.agent_a, "pw-agent-a"), (world.agent_b, "pw-agent-b")):
        admin.execute("update users set password_hash = %s where id = %s", (auth.hash_password(pw), uid))
    # a second agent on store A for claim collisions
    other = admin.execute("insert into users (email,name,password_hash) values ('agent-a2@x.com','Agent A2',%s) returning id",
                          (auth.hash_password("pw-agent-a2"),)).fetchone()[0]
    admin.execute("insert into memberships values (%s,%s,'agent')", (other, world.ta))
    admin.execute("update tenants set shopify_token_enc = %s", (encrypt_token("tok"),))
    stub = StubAgent()
    app.state.agent_factory = lambda tenant: stub
    app.state.shop_factory = lambda tenant: StubShop()
    auth._fails.clear()
    with TestClient(app) as base:
        base.stub = stub
        base.world = world
        yield base


def client_for(email, password):
    c = TestClient(app, headers=H)
    r = c.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return c


def test_login_sets_an_httponly_cookie_and_me_lists_the_stores_and_roles(api):
    r = api.post("/api/auth/login", json={"email": "Admin-A@x.com", "password": "pw-admin-a"}, headers=H)
    assert r.status_code == 200
    assert "httponly" in r.headers["set-cookie"].lower() and "samesite=lax" in r.headers["set-cookie"].lower()
    me = api.get("/api/me").json()
    assert me["user"]["email"] == "admin-a@x.com" and me["stores"] == [{"slug": "store-a", "name": "Store A", "role": "admin"}]


def test_a_wrong_password_and_an_unknown_email_get_the_same_answer(api):
    wrong = api.post("/api/auth/login", json={"email": "admin-a@x.com", "password": "nope"}, headers=H)
    unknown = api.post("/api/auth/login", json={"email": "ghost@x.com", "password": "nope"}, headers=H)
    assert wrong.status_code == unknown.status_code == 401 and wrong.json() == unknown.json()


def test_five_failed_logins_lock_the_account_for_a_while_and_a_right_password_does_not_bypass_it(api):
    for _ in range(5):
        api.post("/api/auth/login", json={"email": "agent-a@x.com", "password": "bad"}, headers=H)
    r = api.post("/api/auth/login", json={"email": "agent-a@x.com", "password": "pw-agent-a"}, headers=H)
    assert r.status_code == 429


def test_nothing_is_reachable_without_a_session(api):
    for path in ("/api/me", "/api/t/store-a/dashboard", "/api/t/store-a/conversations", "/api/t/store-a/handoffs"):
        assert api.get(path).status_code == 401, path


def test_a_tampered_session_cookie_is_refused(api):
    c = client_for("agent-a@x.com", "pw-agent-a")
    assert c.get("/api/me").status_code == 200
    good = c.cookies.get("console_session")
    c.cookies.clear()  # replace the cookie. Setting a second one beside it sends both, and which one the server reads depends on the client's order
    c.cookies.set("console_session", good[:-3] + "xyz")
    assert c.get("/api/me").status_code == 401


def test_another_store_is_a_404_not_a_403(api):
    a = client_for("agent-a@x.com", "pw-agent-a")
    assert a.get("/api/t/store-a/dashboard").status_code == 200  # control
    assert a.get("/api/t/store-b/dashboard").status_code == 404
    assert a.get("/api/t/no-such-store/dashboard").status_code == 404  # same answer as a real store you are not in


def test_state_changing_requests_need_our_own_header_and_origin(api):
    c = TestClient(app)  # no X-Requested-With
    c.cookies.set("console_session", auth.make_session(str(api.world.agent_a)))
    assert c.post("/api/t/store-a/chat", json={"email": "c@x.com", "message": "hi"}).status_code == 403
    hostile = TestClient(app, headers={**H, "Origin": "https://evil.example"})
    hostile.cookies.set("console_session", auth.make_session(str(api.world.agent_a)))
    assert hostile.post("/api/t/store-a/chat", json={"email": "c@x.com", "message": "hi"}).status_code == 403


def test_the_audit_log_and_settings_are_admin_only(api):
    agent, admin = client_for("agent-a@x.com", "pw-agent-a"), client_for("admin-a@x.com", "pw-admin-a")
    assert agent.get("/api/t/store-a/audit").status_code == 403
    assert agent.get("/api/t/store-a/settings").status_code == 403
    assert agent.put("/api/t/store-a/settings/policy", json={"policy_md": "x"}).status_code == 403
    assert admin.get("/api/t/store-a/audit").status_code == 200
    s = admin.get("/api/t/store-a/settings").json()
    # the token never leaves the server: exactly these four fields, and only a boolean about the connection
    assert set(s) == {"name", "shop_domain", "token_connected", "policy_md"} and s["token_connected"] is True


def test_a_chat_turn_is_answered_stored_with_its_steps_and_audited(api):
    c = client_for("agent-a@x.com", "pw-agent-a")
    r = c.post("/api/t/store-a/chat", json={"email": "Miguel@Example.com", "message": "Where is order 1019?"}).json()
    assert r["action"] == "replied" and r["intent"] == "order_status"
    d = c.get("/api/t/store-a/conversations/%s" % r["conversation_id"]).json()
    assert d["conversation"]["status"] == "answered" and d["conversation"]["customer_email"] == "miguel@example.com"
    assert [m["sender"] for m in d["messages"]] == ["customer", "agent"]
    assert "order lookup: found #1019" in d["messages"][1]["meta"]["steps"]
    actions = [a["action"] for a in client_for("admin-a@x.com", "pw-admin-a").get("/api/t/store-a/audit").json()["items"]]
    assert "chat.reply" in actions


def test_an_escalation_opens_a_handoff_and_a_later_message_is_not_answered_over_the_person(api):
    c = client_for("agent-a@x.com", "pw-agent-a")
    r = c.post("/api/t/store-a/chat", json={"email": "angry@x.com", "message": "I will sue you"}).json()
    assert r["action"] == "escalated"
    hand = c.get("/api/t/store-a/handoffs").json()["items"]
    assert [h["reason"] for h in hand if h["customer_email"] == "angry@x.com"] == ["legal"]
    calls_before = len(api.stub.calls)
    r2 = c.post("/api/t/store-a/chat", json={"email": "angry@x.com", "message": "hello??", "conversation_id": r["conversation_id"]}).json()
    assert r2["action"] == "held" and len(api.stub.calls) == calls_before  # the engine was NOT asked


def test_the_inbox_filters_searches_pages_and_never_shows_another_store(api, admin):
    c = client_for("agent-a@x.com", "pw-agent-a")
    for i in range(3):
        c.post("/api/t/store-a/chat", json={"email": "p%d@x.com" % i, "message": "Where is order 1019 batch%d" % i})
    c.post("/api/t/store-a/chat", json={"email": "z@x.com", "message": "I will sue you"})
    allc = c.get("/api/t/store-a/conversations").json()
    assert allc["total"] == 5  # 4 new + the seeded one
    assert c.get("/api/t/store-a/conversations?status=escalated").json()["total"] == 2  # the seeded one + the new one
    assert c.get("/api/t/store-a/conversations?q=batch1").json()["total"] == 1
    assert c.get("/api/t/store-a/conversations?per=2&page=2").json()["items"].__len__() == 2
    assert c.get("/api/t/store-a/conversations?status=bogus").status_code == 422
    assert "cust-b@x.com" not in str(allc)  # store B's seeded conversation is not in A's inbox
    assert c.get("/api/t/store-a/conversations/%s" % api.world.conv_b).status_code == 404
    assert c.get("/api/t/store-a/conversations/not-a-uuid").status_code == 404


def test_claim_collisions_and_who_may_resolve(api):
    a1, a2, adm = (client_for("agent-a@x.com", "pw-agent-a"), client_for("agent-a2@x.com", "pw-agent-a2"),
                   client_for("admin-a@x.com", "pw-admin-a"))
    hid = a1.get("/api/t/store-a/handoffs").json()["items"][0]["id"]
    assert a1.post("/api/t/store-a/handoffs/%s/claim" % hid).status_code == 200
    assert a1.post("/api/t/store-a/handoffs/%s/claim" % hid).status_code == 200      # claiming your own is idempotent
    assert a2.post("/api/t/store-a/handoffs/%s/claim" % hid).status_code == 409      # one clear owner
    assert a2.post("/api/t/store-a/handoffs/%s/resolve" % hid, json={"reply": "mine now"}).status_code == 409
    assert a1.post("/api/t/store-a/handoffs/%s/resolve" % hid, json={"reply": "Sorted, sorry about that.", "note": "refunded by hand"}).status_code == 200
    assert adm.post("/api/t/store-a/handoffs/%s/resolve" % hid, json={"reply": "again"}).status_code == 409   # already resolved
    d = a1.get("/api/t/store-a/conversations/%s" % api.world.conv_a).json()
    assert d["conversation"]["status"] == "resolved" and d["handoff"]["status"] == "resolved"
    assert d["messages"][-1]["sender"] == "human" and d["messages"][-1]["author"] == "Agent A"


def test_a_handoff_of_another_store_cannot_be_claimed(api):
    a = client_for("agent-a@x.com", "pw-agent-a")
    with db.tx(tenant_id=str(api.world.tb), user_id=str(api.world.agent_b), role="agent") as c:
        other = str(c.execute("select id from handoffs").fetchone()["id"])
    assert a.post("/api/t/store-a/handoffs/%s/claim" % other).status_code == 409  # RLS hides it: zero rows, so refused


def test_staff_order_lookups_work_are_validated_and_audited(api):
    a, adm = client_for("agent-a@x.com", "pw-agent-a"), client_for("admin-a@x.com", "pw-admin-a")
    r = a.get("/api/t/store-a/orders/1019").json()
    assert r["found"] and r["email"] == "miguel@example.com" and r["items"] == ["2x USB Audio Interface"]
    assert a.get("/api/t/store-a/orders/9999").json() == {"found": False}
    assert a.get("/api/t/store-a/orders/abc").status_code == 422
    assert a.get("/api/t/store-a/orders/5000").status_code == 502  # an outage is an error, never "not found"
    log = adm.get("/api/t/store-a/audit?action=lookup.order").json()["items"]
    assert {e["detail"]["order"] for e in log} >= {"#1019", "#9999"} and log[0]["actor"] == "agent-a@x.com"


def test_product_lookup_and_policy_edit_round_trip(api, admin):
    a, adm = client_for("agent-a@x.com", "pw-agent-a"), client_for("admin-a@x.com", "pw-admin-a")
    assert a.get("/api/t/store-a/products?q=keyboard").json()["items"][0]["title"] == "65% Mechanical Keyboard"
    assert a.get("/api/t/store-a/products?q=k").status_code == 422
    assert adm.put("/api/t/store-a/settings/policy", json={"policy_md": "## Returns\nforty days"}).status_code == 200
    assert adm.get("/api/t/store-a/settings").json()["policy_md"] == "## Returns\nforty days"
    assert admin.execute("select policy_md from tenants where slug = 'store-b'").fetchone()[0].endswith("ten days")  # B untouched
    assert adm.put("/api/t/store-a/settings/policy", json={"policy_md": "x" * 20001}).status_code == 422
    assert "policy.update" in [e["action"] for e in adm.get("/api/t/store-a/audit").json()["items"]]


def test_the_dashboard_counts_this_stores_week(api):
    c = client_for("agent-a@x.com", "pw-agent-a")
    c.post("/api/t/store-a/chat", json={"email": "d@x.com", "message": "Where is order 1019?"})
    d = c.get("/api/t/store-a/dashboard").json()
    assert d["week"]["answered"] == 1 and d["handoffs"]["open"] == 1 and len(d["days"]) == 7
    assert d["agent_handled_pct"] == 50  # one answered of two this week (the seeded escalated one counts)
