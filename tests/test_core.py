import json

from agent.audit import AuditLog
from agent.core import NEEDS_ORDER_REPLY, NOT_FOUND_REPLY, SupportAgent
from agent.escalation import HOLDING_REPLY
from agent.shopify import ProductFacts

from conftest import FakeShop, ScriptedLLM, TRIAGE_ORDER, order

GOOD = "Your order #1019 is paid and hasn't shipped yet. It was placed 2026-09-20 for 250.0 USD."


def agent(llm, shop=None, policy=None, audit=None):
    return SupportAgent(llm, shop or FakeShop(orders={"1019": ("miguel@example.com", order())}), policy or [], audit)


def test_a_verified_customer_gets_a_grounded_answer():
    llm = ScriptedLLM(TRIAGE_ORDER, [GOOD])
    res = agent(llm).handle("Where is order 1019?", "miguel@example.com")
    assert res.action == "replied" and res.order_name == "#1019" and res.reply == GOOD
    assert "guard: passed on attempt 1" in res.steps


def test_the_wrong_email_gets_no_order_facts_and_no_model_call_for_the_answer():
    llm = ScriptedLLM(TRIAGE_ORDER, [GOOD])
    res = agent(llm).handle("Where is order 1019?", "someone.else@example.com")
    assert res.reply == NOT_FOUND_REPLY
    assert "USB Audio" not in res.reply and "250" not in res.reply
    assert len(llm.calls) == 1  # triage only: the model never saw this order's facts


def test_wrong_email_and_nonexistent_order_are_indistinguishable():
    # Otherwise the lookup is an oracle for which order numbers exist.
    wrong_email = agent(ScriptedLLM(TRIAGE_ORDER)).handle("order 1019?", "x@example.com")
    no_such = agent(ScriptedLLM(dict(TRIAGE_ORDER, order_number="9999"))).handle("order 9999?", "x@example.com")
    assert wrong_email.reply == no_such.reply == NOT_FOUND_REPLY
    assert wrong_email.action == no_such.action == "replied"


def test_a_store_outage_is_an_escalation_not_a_claim_that_the_order_does_not_exist():
    res = agent(ScriptedLLM(TRIAGE_ORDER), FakeShop(error=True)).handle("Where is order 1019?", "miguel@example.com")
    assert res.action == "escalated" and res.escalation_reason == "store lookup failed"
    assert res.reply == HOLDING_REPLY and res.reply != NOT_FOUND_REPLY


def test_a_missing_order_number_is_asked_for():
    res = agent(ScriptedLLM(dict(TRIAGE_ORDER, order_number=None))).handle("where's my stuff", "miguel@example.com")
    assert res.reply == NEEDS_ORDER_REPLY and res.action == "replied"


def test_an_ungrounded_reply_is_retried_with_the_reason_then_accepted():
    llm = ScriptedLLM(TRIAGE_ORDER, ["It will arrive in 4 days.", GOOD])
    res = agent(llm).handle("Where is order 1019?", "miguel@example.com")
    assert res.reply == GOOD and "guard: refused attempt 1" in " ".join(res.steps)
    assert "refused" in llm.calls[-1][1]  # the second prompt carried the refusal reason


def test_two_ungrounded_replies_end_in_a_person_not_a_guess():
    llm = ScriptedLLM(TRIAGE_ORDER, ["It will arrive in 4 days.", "Delivery in 5 days."])
    res = agent(llm).handle("Where is order 1019?", "miguel@example.com")
    assert res.action == "escalated" and res.reply == HOLDING_REPLY
    assert res.escalation_reason == "reply failed the grounding check twice" and res.guard_violations


def test_a_refund_promise_never_reaches_the_customer():
    llm = ScriptedLLM(TRIAGE_ORDER, ["I've issued a refund of 250.0 USD.", "I've issued a refund of 250.0 USD."])
    res = agent(llm).handle("I want my money back for order 1019", "miguel@example.com")
    assert res.action == "escalated" and "refund" not in res.reply.lower().replace("refunds", "")


def test_a_policy_question_is_answered_from_the_policy(policy):
    triage = {"intent": "policy_question", "order_number": None, "product_query": None, "angry": False,
              "legal_threat": False, "chargeback": False, "wants_human": False}
    answer = "You can return unused items within 30 days of delivery."
    res = agent(ScriptedLLM(triage, [answer]), policy=policy).handle("What is your return window?", "a@example.com")
    assert res.action == "replied" and res.reply == answer
    assert any("policy: Returns" in s for s in res.steps)


def test_a_return_on_an_unshipped_order_is_given_the_cancellation_policy(policy):
    triage = dict(TRIAGE_ORDER, intent="return_request")
    llm = ScriptedLLM(triage, ["Your order #1019 hasn't shipped, so it can still be cancelled."])
    agent(llm, policy=policy).handle("I want to return order 1019", "miguel@example.com")
    assert "cancel" in llm.calls[-1][1].lower()  # the cancellation section was in the facts the model saw


def test_a_status_question_is_given_the_shipping_policy(policy):
    llm = ScriptedLLM(TRIAGE_ORDER, [GOOD])
    agent(llm, policy=policy).handle("Where is order 1019?", "miguel@example.com")
    assert "business days" in llm.calls[-1][1]


def test_a_policy_question_with_no_matching_policy_goes_to_a_person(policy):
    triage = {"intent": "policy_question", "order_number": None, "product_query": None, "angry": False,
              "legal_threat": False, "chargeback": False, "wants_human": False}
    res = agent(ScriptedLLM(triage), policy=policy).handle("zzz qqq", "a@example.com")
    assert res.action == "escalated" and res.escalation_reason == "no policy section matched"


def test_a_product_answer_uses_only_catalogue_facts():
    triage = {"intent": "product_question", "order_number": None, "product_query": "keyboard", "angry": False,
              "legal_threat": False, "chargeback": False, "wants_human": False}
    shop = FakeShop(products=[ProductFacts("65% Mechanical Keyboard", "149.0", "USD", True, "Hot-swappable switches.")])
    res = agent(ScriptedLLM(triage, ["The 65% Mechanical Keyboard is 149 USD and in stock."]), shop).handle(
        "Do you have a keyboard?", "a@example.com")
    assert res.action == "replied" and "149" in res.reply


def test_a_model_outage_is_an_escalation():
    res = agent(ScriptedLLM(None, fail_triage=True)).handle("Where is order 1019?", "miguel@example.com")
    assert res.action == "escalated" and res.escalation_reason == "triage failed"


def test_the_audit_log_masks_the_email_and_keeps_the_decision(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    agent(ScriptedLLM(TRIAGE_ORDER, [GOOD]), audit=log).handle("Where is order 1019?", "miguel@example.com")
    line = json.loads((tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert line["customer"] == "m***@example.com" and "miguel" not in json.dumps(line)
    assert line["action"] == "replied" and line["order"] == "#1019" and line["intent"] == "order_status"
