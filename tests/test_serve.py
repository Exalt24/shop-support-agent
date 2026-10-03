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
