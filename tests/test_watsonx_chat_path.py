"""The whole agent on the watsonx provider: the HTTP front (/support), the embeddable widget backend (/api/chat/message) and the
console's engine all build their model through build_llm, so LLM_PROVIDER=watsonx moves every path at once.

The model is tests/watsonx_standin.py, a real HTTP server written from IBM's saved pages. Nothing here is live."""
import json
import time
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import serve
from agent import config, watsonx_llm
from agent.audit import AuditLog
from agent.core import TRIAGE_SYSTEM, SupportAgent
from agent.llm import GroqLLM, build_llm
from agent.watsonx_llm import WatsonxLLM

from conftest import FakeShop, TRIAGE_ORDER, order
from test_enquiry import tri
from watsonx_standin import PROJECT_ID, VALID_KEY, Fault, StandIn, chat_body, http_error

ROOT = Path(__file__).resolve().parent.parent
POLICY_TRIAGE = {"intent": "policy_question", "order_number": None, "product_query": None, "angry": False,
                 "legal_threat": False, "chargeback": False, "wants_human": False}
ORDER_ANSWER = "Your order #1019 is paid and hasn't shipped yet. It was placed 2026-09-20 for 250.0 USD."
POLICY_ANSWER = "You can return unused items within 30 days of delivery."


def responder(triage, answer):
    return lambda system, user: json.dumps(triage) if system == TRIAGE_SYSTEM else answer


@pytest.fixture
def ibm(monkeypatch):
    """A stand-in server (IAM and chat), and settings that point build_llm at it."""
    server = StandIn().start()
    server.sleeps = []  # the client's backoff waits, recorded and not slept
    monkeypatch.setattr(watsonx_llm, "time", types.SimpleNamespace(sleep=server.sleeps.append, time=time.time))
    monkeypatch.setattr(config, "WATSONX_IAM_URL", server.iam_url)
    monkeypatch.setenv("WATSONX_URL", server.base)
    yield server
    server.stop()


@pytest.fixture
def watsonx_env(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SECRETS_DIR", tmp_path)
    for name in ("GROQ_API_KEY", "WATSONX_MODEL_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "watsonx")
    monkeypatch.setenv("WATSONX_API_KEY", VALID_KEY)
    monkeypatch.setenv("WATSONX_PROJECT_ID", PROJECT_ID)
    monkeypatch.setenv("SHOPIFY_SHOP", "demo.myshopify.com")
    monkeypatch.setenv("SHOPIFY_ACCESS_TOKEN", "shpat_x")


def support_app(shop=None):
    return TestClient(serve.create_app(lambda: SupportAgent(build_llm(config.load_settings()), shop or FakeShop(), [], None)))


# ---- /support ----------------------------------------------------------------------------------------------------------

def test_support_answers_an_order_question_through_watsonx(ibm, watsonx_env):
    ibm.responder = responder(TRIAGE_ORDER, ORDER_ANSWER)
    shop = FakeShop(orders={"1019": ("miguel@example.com", order())})
    r = support_app(shop).post("/support", json={"message": "Where is order 1019?", "email": "miguel@example.com"})
    body = r.json()
    assert r.status_code == 200 and body["action"] == "replied" and body["reply"] == ORDER_ANSWER
    triage_call, answer_call = ibm.chat_requests()
    assert triage_call["body"]["messages"][0] == {"role": "system", "content": TRIAGE_SYSTEM} and triage_call["body"]["max_completion_tokens"] == 900
    assert answer_call["body"]["max_completion_tokens"] == 1500 and "FACTS:" in answer_call["body"]["messages"][1]["content"]
    assert all(q["body"]["model_id"] == "ibm/granite-4-h-small" and q["body"]["project_id"] == PROJECT_ID for q in ibm.chat_requests())
    assert len(ibm.iam_requests()) == 1  # two model calls, one token


def test_the_grounding_guard_still_judges_what_watsonx_wrote(ibm, watsonx_env):
    # the same agent, the same guard: a reply with a figure that is not in the facts is refused whichever model wrote it
    ibm.responder = responder(TRIAGE_ORDER, "Your order #1019 will arrive in 2 days and costs 999 USD.")
    shop = FakeShop(orders={"1019": ("miguel@example.com", order())})
    body = support_app(shop).post("/support", json={"message": "Where is order 1019?", "email": "miguel@example.com"}).json()
    assert body["action"] == "escalated" and body["escalation_reason"] == "reply failed the grounding check twice"
    assert "999" not in body["reply"]


@pytest.mark.parametrize("fault,expected", [
    (http_error(503, "service_unavailable", "busy"), "service_unavailable"),
    (http_error(403, "authorization_rejected", "rejected key %s" % VALID_KEY), "authorization_rejected"),
    (http_error(429, "token_quota_reached", "quota used up"), "token_quota_reached"),
    (Fault(200, chat_body(None, "length")), "no text before the limit"),
], ids=["unavailable", "bad-key", "quota", "length"])
def test_a_model_failure_hands_the_case_to_a_person_and_leaks_nothing(ibm, watsonx_env, fault, expected):
    ibm.responder = responder(TRIAGE_ORDER, ORDER_ANSWER)
    support = support_app(FakeShop(orders={"1019": ("miguel@example.com", order())}))
    msg = {"message": "Where is order 1019?", "email": "miguel@example.com"}
    assert support.post("/support", json=msg).json()["action"] == "replied"  # positive control, same request, same server
    token = ibm.chat_requests()[-1]["headers"]["authorization"].split(" ", 1)[1]
    ibm.requests.clear()
    ibm.script_chat(*[fault] * 3)
    body = support.post("/support", json=msg).json()
    assert body["action"] == "escalated" and body["escalation_reason"] == "triage failed"
    assert expected in " | ".join(body["steps"])
    text = json.dumps(body)
    assert VALID_KEY not in text and token not in text


def test_a_transient_overload_is_absorbed_and_the_shopper_never_notices(ibm, watsonx_env):
    ibm.responder = responder(TRIAGE_ORDER, ORDER_ANSWER)
    ibm.script_chat(http_error(503, "service_unavailable"))
    body = support_app(FakeShop(orders={"1019": ("miguel@example.com", order())})).post(
        "/support", json={"message": "Where is order 1019?", "email": "miguel@example.com"}).json()
    # the 503 hit the triage call, which waited 1 s and went again; then the answer call: three requests in all
    assert body["action"] == "replied" and len(ibm.chat_requests()) == 3 and ibm.sleeps == [1.0]


def test_an_expired_token_between_two_messages_is_renewed_without_the_shopper_noticing(ibm, watsonx_env):
    ibm.responder = responder(TRIAGE_ORDER, ORDER_ANSWER)
    support = support_app(FakeShop(orders={"1019": ("miguel@example.com", order())}))
    msg = {"message": "Where is order 1019?", "email": "miguel@example.com"}
    assert support.post("/support", json=msg).json()["action"] == "replied"
    ibm.expire_tokens()
    assert support.post("/support", json=msg).json()["action"] == "replied"
    assert ibm.tokens_issued >= 2


# ---- the widget backend (serve.chat_from_env) --------------------------------------------------------------------------

@pytest.fixture
def widget(ibm, watsonx_env, monkeypatch, tmp_path):
    monkeypatch.setenv("CHAT_TENANTS_FILE", str(ROOT / "widget" / "demo" / "tenants.demo.json"))
    monkeypatch.setenv("CHAT_SIGNING_KEY", "k" * 40)
    for tenant in ("HARBOUR", "LOOMHOUSE"):
        for name in ("CLIENT_ID", "CLIENT_SECRET", "REFRESH_TOKEN"):
            monkeypatch.setenv("ZOHO_%s_%s" % (tenant, name), "x")
    monkeypatch.setattr(serve, "AuditLog", lambda path: AuditLog(tmp_path / "audit.jsonl"))
    return TestClient(serve.create_app(chat=serve.chat_from_env()))


def say(client, message):
    return client.post("/api/chat/message", json={"site": "pk_harbour_demo_01", "message": message},
                       headers={"Origin": "http://127.0.0.1:8101"})


def test_the_widget_backend_runs_on_watsonx_when_the_provider_is_set(widget, ibm):
    ibm.responder = responder(tri("general", legacy="policy_question"), POLICY_ANSWER)
    r = say(widget, "What is your returns policy?")
    assert r.status_code == 200 and r.json()["reply"] == POLICY_ANSWER and r.json()["action"] == "replied"
    assert ibm.chat_requests() and all(q["headers"]["authorization"].startswith("Bearer tok-") for q in ibm.chat_requests())


def test_the_widget_backend_hands_a_watsonx_outage_to_a_person(widget, ibm):
    ibm.responder = responder(tri("general", legacy="policy_question"), POLICY_ANSWER)
    assert say(widget, "What is your returns policy?").json()["action"] == "replied"  # positive control
    ibm.script_chat(*[http_error(500, "internal_error", "down")] * 3)
    r = say(widget, "What is your returns policy?")
    assert r.status_code == 200 and r.json()["action"] == "escalated" and VALID_KEY not in r.text


# ---- the other entry points --------------------------------------------------------------------------------------------

TENANT_POLICY = "## Returns" + chr(10) + "thirty days"


def console_agent(monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("CONSOLE_FERNET_KEY", Fernet.generate_key().decode())  # so the real secrets folder is neither read nor written
    from console_api.engine import build_agent
    from console_api.secrets_store import encrypt_token
    return build_agent({"shop_domain": "a.myshopify.com", "shopify_token_enc": encrypt_token("tok"), "policy_md": TENANT_POLICY})


def test_the_console_engine_builds_its_agent_on_watsonx(watsonx_env, monkeypatch):
    agent = console_agent(monkeypatch)
    assert isinstance(agent._llm, WatsonxLLM) and agent._llm._key == VALID_KEY and agent._llm._project == PROJECT_ID


def test_the_console_engine_stays_on_groq_when_nothing_is_set(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SECRETS_DIR", tmp_path)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    assert isinstance(console_agent(monkeypatch)._llm, GroqLLM)


def test_the_default_agent_builder_uses_the_provider_setting(ibm, watsonx_env):
    ibm.responder = responder(POLICY_TRIAGE, POLICY_ANSWER)
    agent = serve.build_default_agent()
    assert isinstance(agent._llm, WatsonxLLM)
