import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json

import pytest

from agent.core import TRIAGE_SYSTEM
from agent.policy import load_policy
from agent.shopify import OrderFacts, ProductFacts, ShopifyError


class ScriptedLLM:
    """Returns the scripted triage JSON for the triage prompt and the next scripted answer otherwise."""

    def __init__(self, triage: dict | None, answers: list[str] | None = None, fail_triage: bool = False):
        self.triage = triage
        self.answers = list(answers or [])
        self.fail_triage = fail_triage
        self.calls: list[tuple[str, str]] = []

    def complete(self, system, user, *, max_tokens=1800):
        from agent.llm import LLMError
        self.calls.append((system, user))
        if system == TRIAGE_SYSTEM:
            if self.fail_triage:
                raise LLMError("scripted triage failure")
            return json.dumps(self.triage)
        if not self.answers:
            raise LLMError("no scripted answer left")
        return self.answers.pop(0)


class NoLLM:
    """Any call is a test failure: proves a path never reached the model."""

    def complete(self, system, user, *, max_tokens=1800):
        raise AssertionError("the model must not be called on this path")


class FakeShop:
    def __init__(self, orders=None, products=None, error=False):
        self.orders = orders or {}  # order number -> (email, OrderFacts)
        self.products = products or []
        self.error = error

    def find_order(self, order_name, email):
        if self.error:
            raise ShopifyError("scripted outage")
        entry = self.orders.get(order_name.lstrip("#"))
        if not entry or entry[0].lower() != email.lower():
            return None
        return entry[1]

    def search_products(self, text, limit=3):
        if self.error:
            raise ShopifyError("scripted outage")
        return list(self.products)


def order(name="#1019", status="unfulfilled", tracking=()):
    return OrderFacts(name=name, created_at="2026-09-20T10:00:00Z", financial_status="paid", fulfillment_status=status,
                      total="250.0", currency="USD", items=("2x USB Audio Interface",), tracking=tuple(tracking))


TRIAGE_ORDER = {"intent": "order_status", "order_number": "1019", "product_query": None,
                "angry": False, "legal_threat": False, "chargeback": False, "wants_human": False}


@pytest.fixture
def policy():
    return load_policy(Path(__file__).resolve().parent.parent / "policy.md")
