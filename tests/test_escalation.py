import pytest

from agent.core import SupportAgent
from agent.escalation import HOLDING_REPLY, decide, hard_reason

from conftest import FakeShop, NoLLM, ScriptedLLM, TRIAGE_ORDER, order


@pytest.mark.parametrize("message,reason", [
    ("I will contact my lawyer about this", "legal"),
    ("This is a chargeback waiting to happen", "payment dispute"),
    ("I was charged twice for order 1019", "payment dispute"),
    ("Let me speak to a manager please", "asked for a person"),
    ("The battery caught fire", "safety"),
])
def test_hard_triggers(message, reason):
    assert hard_reason(message) == reason


def test_a_calm_order_question_is_not_escalated():
    assert decide("Where is my order 1019?", {"angry": False}) is None


def test_the_model_vote_can_escalate_but_never_overrule_the_floor():
    assert decide("my order is late", {"angry": True}) == "model flagged: angry"
    # the model says everything is fine, the keyword floor still wins
    assert decide("I'm calling my attorney", {"angry": False, "legal_threat": False}) == "legal"


def test_a_legal_threat_never_reaches_the_model():
    agent = SupportAgent(NoLLM(), FakeShop(), [])
    res = agent.handle("My solicitor will be in touch about order 1019", "a@example.com")
    assert res.action == "escalated" and res.reply == HOLDING_REPLY and res.escalation_reason == "legal"


def test_model_flagged_anger_escalates_with_no_lookup():
    shop = FakeShop(orders={"1019": ("a@example.com", order())})
    llm = ScriptedLLM(dict(TRIAGE_ORDER, angry=True))
    res = SupportAgent(llm, shop, []).handle("This is the worst, where is order 1019", "a@example.com")
    assert res.action == "escalated" and res.escalation_reason == "model flagged: angry"
    assert len(llm.calls) == 1  # triage only, no answer was written
