"""The chat endpoints behind the widget, through FastAPI's test client: two tenants, a scripted model, a fake Zoho each.

Nothing here is live. The browser side of the same flows is tests/test_widget_browser.py."""
import json
import secrets

import pytest
from fastapi.testclient import TestClient

from agent.chat_api import ChatBackend, MAX_BODY_BYTES
from agent.chat_guard import ConversationSigner
from agent.core import Result, SupportAgent
from agent.tenants import TenantRegistry, desk_for
from agent.zoho_crm import ZohoCrmClient, ZohoSettings
from agent.zoho_fake import FakeZoho
from demo_widget import KeywordModel, NoShop, tenants_for
from serve import create_app

A, B = "https://harbour.example.test", "https://loomhouse.example.test"
KEY_A, KEY_B = "pk_harbour_demo_01", "pk_loomhouse_demo_02"
QUOTE = "Hi, can I get a quote to replace our boiler? My name is Tom Reyes and my email is tom.reyes@example.com"
QUOTE_B = "Hello, can I get a quote for 40 towels for our hotel? My name is Ana Cruz and my email is ana.cruz@example.com"
YES = "Yes, please go ahead."


class Rig:
    def __init__(self, **options):
        self.registry = TenantRegistry.from_dict(tenants_for({"harbour-heating": A, "loomhouse-homewares": B}))
        self.zoho = {t.zoho: FakeZoho() for t in self.registry.tenants}
        self.made = []

        def agent_for(tenant):
            self.made.append(tenant.id)
            crm = ZohoCrmClient(ZohoSettings("c", "s", "r"), self.zoho[tenant.zoho], sleep=lambda s: None)
            return SupportAgent(KeywordModel(), NoShop(), [], None, None, desk_for(tenant, crm))

        self.signer = ConversationSigner(secrets.token_hex(32).encode())
        self.backend = ChatBackend(self.registry, self.signer, agent_for, **options)
        self.http = TestClient(create_app(chat=self.backend))

    def say(self, message, site=KEY_A, origin=A, token=None, **extra):
        body = {"site": site, "message": message, **({"conversation_id": token} if token else {}), **extra}
        return self.http.post("/api/chat/message", json=body, headers={"Origin": origin})


def rig(**options):
    return Rig(**options)


# ---- config and CORS --------------------------------------------------------------------------------------------------------

def test_config_gives_the_tenants_look_to_its_own_origin_and_nothing_secret():
    r = rig().http.get("/api/chat/config", params={"site": KEY_A}, headers={"Origin": A})
    body = r.json()
    assert r.status_code == 200 and body["name"] == "Harbour Heating" and body["primary"] == "#0b5d7a" and body["initials"] == "HH"
    assert set(body) == {"name", "initials", "primary", "on_primary", "greeting"}
    assert r.headers["access-control-allow-origin"] == A and r.headers["vary"] == "Origin"
    assert "access-control-allow-credentials" not in r.headers and "set-cookie" not in r.headers
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("origin", ["https://evil.example.test", "null", "", "https://harbour.example.test.evil.test", "http://harbour.example.test"])
def test_an_origin_no_tenant_lists_is_refused_with_no_cors_headers(origin):
    r = rig().http.get("/api/chat/config", params={"site": KEY_A}, headers={"Origin": origin} if origin else {})
    assert r.status_code == 403 and r.json() == {"error": "origin_not_allowed"}
    assert not any(h.lower().startswith("access-control") for h in r.headers)


def test_one_tenants_key_from_another_tenants_origin_is_refused():
    t = rig()
    assert t.http.get("/api/chat/config", params={"site": KEY_B}, headers={"Origin": A}).status_code == 403
    assert t.say("hello", site=KEY_B, origin=A).status_code == 403
    assert t.zoho["loomhouse"].calls == [] and t.made == []  # the agent was never even built


def test_an_unknown_site_key_is_404_and_a_listed_origin_still_gets_the_answer_readable():
    r = rig().http.get("/api/chat/config", params={"site": "pk_nothing_here_1"}, headers={"Origin": A})
    assert r.status_code == 404 and r.headers["access-control-allow-origin"] == A


def test_preflight_is_answered_for_listed_origins_only():
    t = rig()
    ok = t.http.options("/api/chat/message", headers={"Origin": B, "Access-Control-Request-Method": "POST",
                                                      "Access-Control-Request-Headers": "content-type"})
    assert ok.status_code == 204 and ok.headers["access-control-allow-origin"] == B
    assert ok.headers["access-control-allow-methods"] == "GET, POST" and ok.headers["access-control-allow-headers"] == "Content-Type"
    assert "access-control-allow-credentials" not in ok.headers
    bad = t.http.options("/api/chat/message", headers={"Origin": "https://evil.example.test", "Access-Control-Request-Method": "POST"})
    assert bad.status_code == 403 and "access-control-allow-origin" not in bad.headers


# ---- the conversation: routing, consent, a lead for the right tenant -------------------------------------------------------

def test_a_quote_is_routed_asks_for_consent_and_a_yes_creates_a_lead_for_that_tenant_only():
    t = rig()
    first = t.say(QUOTE).json()
    assert first["awaiting_consent"] is True and "estimating team" in first["reply"] and first["action"] == "replied"
    assert t.zoho["harbour"].leads == {}                       # nothing before the yes
    second = t.say(YES, token=first["conversation_id"]).json()
    assert second["awaiting_consent"] is False and "estimating team" in second["reply"]
    (lead,) = t.zoho["harbour"].leads.values()
    assert (lead["First_Name"], lead["Last_Name"], lead["Email"]) == ("Tom", "Reyes", "tom.reyes@example.com")
    (task,) = t.zoho["harbour"].tasks
    assert task["Subject"].startswith("Prepare a quote") and task["_lar_id"] == "LAR-HARBOUR-ESTIMATING"
    assert "estimating queue" in t.zoho["harbour"].notes[0]["content"]
    assert t.zoho["loomhouse"].calls == []                     # the other site's CRM was never touched


def test_the_same_kind_of_enquiry_goes_to_a_different_owner_queue_on_the_other_site():
    t = rig()
    a = t.say(QUOTE).json()
    t.say(YES, token=a["conversation_id"])
    b = t.say(QUOTE_B, site=KEY_B, origin=B).json()
    assert "trade accounts team" in b["reply"]
    t.say(YES, site=KEY_B, origin=B, token=b["conversation_id"])
    (lead_b,) = t.zoho["loomhouse"].leads.values()
    assert lead_b["Last_Name"] == "Cruz"
    (task_b,) = t.zoho["loomhouse"].tasks
    assert task_b["_lar_id"] == "LAR-LOOMHOUSE-TRADE" and task_b["Subject"].startswith("Prepare a quote")
    assert "trade-accounts queue" in t.zoho["loomhouse"].notes[0]["content"]
    assert len(t.zoho["harbour"].leads) == 1 and len(t.zoho["harbour"].tasks) == 1


def test_a_service_request_has_its_own_queue_per_site_and_an_unlisted_intent_keeps_the_default():
    t = rig()
    a = t.say("My boiler is not working, can someone visit? My name is Sam Lee and my phone is 0917 123 4567").json()
    assert "dispatch team" in a["reply"]
    t.say(YES, token=a["conversation_id"])
    (lead,) = t.zoho["harbour"].leads.values()
    assert "Email" not in lead and lead["Phone"] == "09171234567"   # a widget visitor gives their own details: none are assumed
    b = t.say("We want to buy in bulk. My name is Ana Cruz and my email is ana.cruz@example.com", site=KEY_B, origin=B).json()
    assert "web sales team" in b["reply"]


def test_the_consent_buttons_cover_yes_no_and_no_answer_at_all():
    t = rig()
    q = t.say(QUOTE).json()
    assert q["awaiting_consent"] is True
    odd = t.say("what about the weather", token=q["conversation_id"]).json()
    assert odd["awaiting_consent"] is True and "haven't passed your details on" in odd["reply"]  # still open: the buttons stay
    no = t.say("No, thanks.", token=q["conversation_id"]).json()
    assert no["awaiting_consent"] is False and "won't pass your details" in no["reply"]
    assert t.zoho["harbour"].leads == {}


def test_a_visitor_cannot_use_another_visitors_conversation():
    t = rig()
    mine = t.say(QUOTE).json()["conversation_id"]
    # a second visitor with no id starts their own conversation: their "yes" answers a question nobody asked them
    other = t.say(YES).json()
    assert other["conversation_id"] != mine and other["awaiting_consent"] is False
    assert t.zoho["harbour"].leads == {}
    # an id they edit, or one issued for the other site, is refused
    assert t.say(YES, token=mine[:-2] + "xx").status_code == 400
    assert t.say(YES, token=mine, site=KEY_B, origin=B).json() == {"error": "bad_conversation"}
    assert t.say(YES, token="not-a-token").status_code == 400
    assert t.zoho["harbour"].leads == {}


def test_a_replayed_yes_does_not_make_a_second_lead():
    t = rig()
    tok = t.say(QUOTE).json()["conversation_id"]
    t.say(YES, token=tok)
    t.say(YES, token=tok)
    assert len(t.zoho["harbour"].leads) == 1 and len(t.zoho["harbour"].tasks) == 1


# ---- limits and hostile input ------------------------------------------------------------------------------------------------

def test_the_per_ip_limit_answers_429_with_a_wait_and_stays_readable_by_the_page():
    t = rig(ip_per_minute=3, conversation_per_minute=99)
    codes = [t.say("hello there").status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    r = t.say("hello there")
    assert r.json()["error"] == "slow_down" and int(r.headers["retry-after"]) >= 1 and r.headers["access-control-allow-origin"] == A


def test_the_per_conversation_limit_is_separate_from_the_ip_limit():
    t = rig(ip_per_minute=99, conversation_per_minute=2)
    tok = t.say("hello there").json()["conversation_id"]
    assert t.say("hello again", token=tok).status_code == 200
    assert t.say("and again", token=tok).status_code == 429
    assert t.say("a new visitor").status_code == 200          # another conversation from the same address is fine


def test_the_per_ip_limit_cannot_be_dodged_with_a_forged_forwarding_header_unless_a_proxy_is_trusted():
    t = rig(ip_per_minute=2, conversation_per_minute=99)
    codes = [t.http.post("/api/chat/message", json={"site": KEY_A, "message": "hi there"},
                         headers={"Origin": A, "X-Forwarded-For": "9.9.9.%d" % i}).status_code for i in range(3)]
    assert codes == [200, 200, 429]
    p = rig(ip_per_minute=2, conversation_per_minute=99, trust_proxy=True)
    codes = [p.http.post("/api/chat/message", json={"site": KEY_A, "message": "hi there"},
                         headers={"Origin": A, "X-Forwarded-For": "1.1.1.1, 9.9.9.%d" % i}).status_code for i in range(3)]
    assert codes == [200, 200, 200]  # behind a proxy the LAST entry is the real address: these are three different ones


def test_a_body_over_the_cap_is_refused_unread_and_a_streamed_one_is_cut_off():
    t = rig()
    big = json.dumps({"site": KEY_A, "message": "x" * (MAX_BODY_BYTES + 10)})
    assert t.http.post("/api/chat/message", content=big, headers={"Origin": A, "Content-Type": "application/json"}).status_code == 413

    def stream():  # no Content-Length: the body is chunked
        yield b'{"site": "' + KEY_A.encode() + b'", "message": "'
        for _ in range(MAX_BODY_BYTES // 1024 + 2):
            yield b"y" * 1024
        yield b'"}'

    r = t.http.post("/api/chat/message", content=stream(), headers={"Origin": A, "Content-Type": "application/json"})
    assert r.status_code == 413 and t.made == []


def test_a_declared_length_over_the_cap_is_refused_before_the_body_is_read():
    t = rig()
    small = json.dumps({"site": KEY_A, "message": "hello"})  # the body is fine; only the header lies
    r = t.http.post("/api/chat/message", content=small, headers={"Origin": A, "Content-Type": "application/json",
                                                                 "Content-Length": str(MAX_BODY_BYTES + 500)})
    assert r.status_code == 413 and t.made == []


def test_a_long_message_a_blank_one_extra_fields_and_bad_json_get_a_fixed_answer_that_echoes_nothing():
    t = rig()
    hostile = "<script>alert(1)</script>"
    for body in ({"site": KEY_A, "message": hostile * 100},          # over 1000 characters
                 {"site": KEY_A, "message": "   "},
                 {"site": KEY_A, "message": hostile, "admin": True},  # a field the API does not have
                 {"site": KEY_A},
                 {"message": hostile}):
        r = t.http.post("/api/chat/message", json=body, headers={"Origin": A})
        assert r.status_code == 400 and r.json() == {"error": "bad_request"}, body
        assert "script" not in r.text
    raw = t.http.post("/api/chat/message", content=b'{"site": "' + hostile.encode() + b'"', headers={"Origin": A, "Content-Type": "application/json"})
    assert raw.status_code == 400 and "script" not in raw.text
    unknown = t.say("hi", site="pk_" + "x" * 20)
    assert unknown.status_code == 404 and unknown.json() == {"error": "unknown_site"}


def test_only_json_is_accepted():
    r = rig().http.post("/api/chat/message", content="site=x&message=y", headers={"Origin": A, "Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 415


def test_no_cookie_is_set_and_one_sent_by_the_visitor_changes_nothing():
    t = rig()
    r = t.http.post("/api/chat/message", json={"site": KEY_A, "message": "hello"}, headers={"Origin": A, "Cookie": "session=abc"})
    assert r.status_code == 200 and "set-cookie" not in r.headers and "access-control-allow-credentials" not in r.headers


def test_the_reply_is_json_text_with_control_characters_removed_and_markup_left_as_written():
    t = rig()

    class Rude:
        def handle(self, message, email, conversation_id=None):
            return Result("replied", "hi \x00\x1b[0m<img src=x onerror=alert(1)>\nsecond line")

    t.backend._agents["harbour-heating"] = Rude()
    r = t.say("anything")
    assert r.headers["content-type"].startswith("application/json") and r.headers["x-content-type-options"] == "nosniff"
    assert r.json()["reply"] == "hi [0m<img src=x onerror=alert(1)>\nsecond line"  # drawn as text by the widget; the control characters are gone
    assert "default-src 'none'" in r.headers["content-security-policy"]


def test_a_crash_inside_the_agent_is_a_502_with_no_detail():
    t = rig()

    class Broken:
        def handle(self, *a):
            raise RuntimeError("secret internals " + QUOTE)

    t.backend._agents["harbour-heating"] = Broken()
    r = t.say("hi")
    assert r.status_code == 502 and r.json() == {"error": "unavailable"} and "secret" not in r.text


def test_an_escalation_is_returned_as_one_and_the_widget_gets_no_buttons():
    r = rig().say("I will sue you, my lawyer will call").json()
    assert r["action"] == "escalated" and r["awaiting_consent"] is False


def test_the_widget_script_is_served_with_the_right_type_and_never_writes_html():
    r = rig().http.get("/widget/widget.js")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/javascript")
    assert "innerHTML" not in r.text and "document.write" not in r.text and "eval(" not in r.text
