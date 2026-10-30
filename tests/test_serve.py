from fastapi.testclient import TestClient

from agent.core import SupportAgent
from serve import create_app

from conftest import FakeShop, ScriptedLLM, TRIAGE_ORDER, order

GOOD = "Your order #1019 is paid and hasn't shipped yet. It was placed 2026-09-20 for 250.0 USD."


def make(api_key=None):
    shop = FakeShop(orders={"1019": ("miguel@example.com", order())})
    return TestClient(create_app(lambda: SupportAgent(ScriptedLLM(TRIAGE_ORDER, [GOOD]), shop, []), api_key=api_key))


def test_health():
    assert make().get("/health").json() == {"ok": True}


def test_support_returns_the_agent_result():
    r = make().post("/support", json={"message": "Where is order 1019?", "email": "miguel@example.com"})
    body = r.json()
    assert r.status_code == 200 and body["action"] == "replied" and body["order"] == "#1019" and body["reply"] == GOOD


def test_a_missing_key_is_refused_when_a_key_is_configured():
    c = make(api_key="s3cret")
    assert c.post("/support", json={"message": "hi", "email": "a@example.com"}).status_code == 401
    assert c.post("/support", json={"message": "Where is order 1019?", "email": "miguel@example.com"},
                  headers={"X-Agent-Key": "s3cret"}).status_code == 200


def test_an_empty_message_is_rejected():
    assert make().post("/support", json={"message": "", "email": "a@example.com"}).status_code == 422


def test_enquiry_routing_and_a_lead_through_the_http_front():
    from agent.enquiry import EnquiryDesk
    from agent.leads import LeadCapture
    from agent.zoho_crm import ZohoCrmClient, ZohoSettings
    from agent.zoho_fake import FakeZoho
    from test_enquiry import FULL, QUOTE_MSG, TurnLLM, tri

    http = FakeZoho()
    desk = EnquiryDesk(LeadCapture(ZohoCrmClient(ZohoSettings("c", "s", "r"), http, sleep=lambda s: None), tenant="t"), tenant="t")
    llm = TurnLLM([tri("quote_request", FULL), tri("general")])
    c = TestClient(create_app(lambda: SupportAgent(llm, FakeShop(), [], None, None, desk)))
    body = {"message": QUOTE_MSG, "email": "jane.doe@example.com", "conversation_id": "w1"}
    first = c.post("/support", json=body).json()
    assert first["enquiry"]["queue"] == "sales" and "May I pass your name" in first["reply"] and http.leads == {}
    second = c.post("/support", json=dict(body, message="Yes please")).json()
    assert second["enquiry"]["lead"] == "created" and len(http.leads) == 1


def test_no_zoho_variables_means_no_desk_and_the_old_behaviour():
    from agent.leads import desk_from_env
    assert desk_from_env({}) is None and desk_from_env({"ZOHO_CLIENT_ID": "x"}) is None
    desk = desk_from_env({"ZOHO_CLIENT_ID": "a", "ZOHO_CLIENT_SECRET": "b", "ZOHO_REFRESH_TOKEN": "c", "ZOHO_REGION": "au",
                          "SUPPORT_TENANT": "shop-a", "ZOHO_ASSIGNMENT_RULES": "sales=9, nope=1, service=7"})
    assert desk.tenant == "shop-a" and desk.capture._rules == {"sales": "9", "service": "7"}
