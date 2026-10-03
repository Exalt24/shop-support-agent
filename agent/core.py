"""The support agent: triage, look up, answer from the facts, check the answer, or hand to a person.

The order of operations is the design:
  1. escalation rules run first, before any lookup, so a legal threat never reaches the model's reply path;
  2. facts come from Shopify and the policy file, never from the model's memory;
  3. the model only writes the wording, and the grounding guard refuses any figure or commitment
     the facts do not contain;
  4. anything that fails on the way (a lookup, the model, the guard) ends in a human, never in a guess.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .audit import AuditLog, mask_email
from .escalation import HOLDING_REPLY, decide
from .guard import figures, verify_grounded
from .llm import LLM, LLMError, parse_json
from .policy import PolicySection, retrieve
from .shopify import Shop, ShopifyError

TRIAGE_SYSTEM = """You triage customer messages for an online store. Reply with ONE JSON object and nothing else:
{"intent": "order_status" | "return_request" | "policy_question" | "product_question" | "other",
 "order_number": "<digits only, or null>",
 "product_query": "<short product search text, or null>",
 "angry": true|false, "legal_threat": true|false, "chargeback": true|false, "wants_human": true|false}
Use null when a value is not in the message. Do not answer the customer."""

ANSWER_SYSTEM = """You are a friendly, concise customer support agent for an online store.
Answer ONLY from the FACTS below. Rules:
- Use no figure, date, price, quantity or tracking number that is not in the FACTS.
- If the FACTS do not answer the question, say a teammate will follow up. Do not guess.
- You cannot approve refunds, offer discounts, promise replacements or change orders. Explain the policy and the next step instead.
- Describe an outcome (a refund, a cancellation, an email sent) only if the FACTS state that exact outcome. Never say something has already happened unless the FACTS say it did.
- Two to four sentences. No greeting line, no sign-off."""

NEEDS_ORDER_REPLY = "Happy to help. Could you send me your order number (it looks like #1001)?"
NOT_FOUND_REPLY = ("I couldn't find that order under this email address. Please check the order number, and that you're "
                   "writing from the email you used at checkout.")
NO_PRODUCT_REPLY = "I couldn't find that product in our catalogue. Could you tell me a bit more about what you're after?"


@dataclass
class Result:
    action: str  # "replied" or "escalated"
    reply: str
    intent: str = "other"
    order_name: str | None = None
    escalation_reason: str | None = None
    guard_violations: tuple[str, ...] = ()
    steps: list[str] = field(default_factory=list)


class SupportAgent:
    def __init__(self, llm: LLM, shop: Shop, policy: list[PolicySection], audit: AuditLog | None = None):
        self._llm, self._shop, self._policy, self._audit = llm, shop, policy, audit

    # -- public -----------------------------------------------------------------------------

    def handle(self, message: str, email: str) -> Result:
        res = self._run(message, email)
        if self._audit:
            self._audit.write({
                "customer": mask_email(email), "message_chars": len(message), "intent": res.intent,
                "order": res.order_name, "action": res.action, "escalation_reason": res.escalation_reason,
                "guard_violations": list(res.guard_violations), "steps": res.steps})
        return res

    # -- internals --------------------------------------------------------------------------

    def _escalate(self, res: Result, reason: str) -> Result:
        res.action, res.reply, res.escalation_reason = "escalated", HOLDING_REPLY, reason
        res.steps.append("escalated: " + reason)
        return res

    def _run(self, message: str, email: str) -> Result:
        res = Result(action="replied", reply="")

        # 1. escalation floor, before anything else touches the message
        hard = decide(message, {})
        if hard:
            res.steps.append("hard escalation rule matched before triage")
            return self._escalate(res, hard)

        # 2. triage
        try:
            triage = parse_json(self._llm.complete(TRIAGE_SYSTEM, message, max_tokens=900))
        except LLMError as e:
            res.steps.append("triage failed: %s" % e)
            return self._escalate(res, "triage failed")
        res.intent = str(triage.get("intent") or "other")
        res.steps.append("triage: intent=%s" % res.intent)
        reason = decide(message, triage)
        if reason:
            return self._escalate(res, reason)

        # 3. facts
        facts: list[str] = []
        try:
            if res.intent in ("order_status", "return_request"):
                number = re.sub(r"\D", "", str(triage.get("order_number") or ""))
                if not number:
                    res.reply = NEEDS_ORDER_REPLY
                    res.steps.append("asked for the order number")
                    return res
                order = self._shop.find_order(number, email)
                if order is None:
                    res.order_name = "#" + number
                    res.reply = NOT_FOUND_REPLY
                    res.steps.append("order lookup: none for this email (identical answer for 'no such order')")
                    return res
                res.order_name = order.name
                res.steps.append("order lookup: found %s" % order.name)
                facts.append(order.as_text())
                if res.intent == "return_request":
                    facts += [s.as_text() for s in retrieve("returns return refund shipping", self._policy)]
                    if order.fulfillment_status == "unfulfilled":
                        # not shipped yet: the useful answer is that it can still be changed or cancelled
                        facts += [s.as_text() for s in retrieve("order changes cancellations unfulfilled", self._policy, k=1)]
                else:
                    facts += [s.as_text() for s in retrieve("shipping times delivery tracking", self._policy, k=1)]
            elif res.intent == "policy_question":
                hits = retrieve(message, self._policy)
                if not hits:
                    return self._escalate(res, "no policy section matched")
                res.steps.append("policy: " + ", ".join(h.heading for h in hits))
                facts += [h.as_text() for h in hits]
            elif res.intent == "product_question":
                products = self._shop.search_products(str(triage.get("product_query") or message))
                if not products:
                    res.reply = NO_PRODUCT_REPLY
                    res.steps.append("product search: no match")
                    return res
                res.steps.append("product search: %d match(es)" % len(products))
                facts += [p.as_text() for p in products]
            else:
                return self._escalate(res, "out of scope")
        except ShopifyError as e:
            res.steps.append("shopify error: %s" % e)
            return self._escalate(res, "store lookup failed")

        # 4. write the reply, 5. refuse it if it is not grounded
        facts_text = "\n".join(facts)
        allowed_extra = frozenset(figures(message))
        feedback = ""
        for attempt in (1, 2):
            try:
                reply = self._llm.complete(
                    ANSWER_SYSTEM, "FACTS:\n%s\n\nCUSTOMER MESSAGE:\n%s%s" % (facts_text, message, feedback),
                    max_tokens=1500)
            except LLMError as e:
                res.steps.append("answer failed: %s" % e)
                return self._escalate(res, "answer generation failed")
            check = verify_grounded(reply, facts_text, allowed_extra=allowed_extra)
            if check.ok:
                res.reply = reply
                res.steps.append("guard: passed on attempt %d" % attempt)
                return res
            res.guard_violations = check.violations
            res.steps.append("guard: refused attempt %d (%s)" % (attempt, "; ".join(check.violations)))
            feedback = "\n\nYour last reply was refused: %s. Rewrite it using only the FACTS." % "; ".join(check.violations)
        return self._escalate(res, "reply failed the grounding check twice")
