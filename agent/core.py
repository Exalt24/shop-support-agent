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
from .enquiry import EnquiryDesk, EnquiryInfo
from .escalation import HOLDING_REPLY, decide
from .guard import figures, verify_grounded
from .llm import LLM, LLMError, parse_json
from .memory import MAX_PER_TURN, Candidate, MemoryStore, clean_candidate, format_hints, last_order_number
from .policy import PolicySection, retrieve
from .shopify import Shop, ShopifyError

TRIAGE_SYSTEM = """You triage customer messages for an online store. Reply with ONE JSON object and nothing else:
{"intent": "order_status" | "return_request" | "policy_question" | "product_question" | "other",
 "order_number": "<digits only, or null>",
 "product_query": "<short product search text, or null>",
 "enquiry_intent": "order_support" | "product_question" | "sales_enquiry" | "quote_request" | "service_request" | "general" | "human_needed",
 "lead": {"name": "<the customer's name as they wrote it, or null>", "email": "<as written, or null>", "phone": "<as written, or null>",
          "company": "<only if they named it, or null>"},
 "angry": true|false, "legal_threat": true|false, "chargeback": true|false, "wants_human": true|false}
Use null when a value is not in the message. Copy lead values exactly as the customer wrote them and never make one up.
Do not answer the customer."""

ANSWER_SYSTEM = """You are a friendly, concise customer support agent for an online store.
Answer ONLY from the FACTS below. Rules:
- Use no figure, date, price, quantity or tracking number that is not in the FACTS.
- If the FACTS do not answer the question, say a teammate will follow up. Do not guess.
- You cannot approve refunds, offer discounts, promise replacements or change orders. Explain the policy and the next step instead.
- Describe an outcome (a refund, a cancellation, an email sent) only if the FACTS state that exact outcome. Never say something has already happened unless the FACTS say it did.
- Two to four sentences. No greeting line, no sign-off."""

MEMORY_EXTRACT_SYSTEM = """From the customer's message below, pick out at most three lasting things THEY said about themselves that a support
agent should remember next time: a contact preference, or a plain fact about them. Reply with ONE JSON object and nothing else:
{"memories": [{"kind": "preference" | "fact", "key": "short_snake_case_name", "body": "their own words, under 120 characters"}]}
Use only words the customer wrote. Never include order numbers, prices, dates, card numbers, passwords or anything about a
complaint or a refund. If there is nothing worth remembering, reply {"memories": []}."""

ORDER_WORDS = {"order_status": "asked where order %s was", "return_request": "asked about returning order %s"}

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
    enquiry: EnquiryInfo | None = None  # set only when an EnquiryDesk is attached


class SupportAgent:
    def __init__(self, llm: LLM, shop: Shop, policy: list[PolicySection], audit: AuditLog | None = None,
                 memory: MemoryStore | None = None, enquiry: EnquiryDesk | None = None):
        self._llm, self._shop, self._policy, self._audit, self._memory = llm, shop, policy, audit, memory
        self._enquiry = enquiry  # None: the agent behaves exactly as it did before enquiry routing existed

    def attach_memory(self, memory: MemoryStore | None) -> None:
        """Memory is per request (it carries the tenant and the staff member), so the console attaches it per turn."""
        self._memory = memory

    # -- public -----------------------------------------------------------------------------

    def handle(self, message: str, email: str, conversation_id: str | None = None) -> Result:
        res = self._run(message, email, conversation_id)
        try:
            self._capture_lead(res, email, conversation_id)
        except Exception as e:  # the lead is a convenience for the team: whatever goes wrong, the reply is already final
            res.steps.append("lead capture error: %s (the reply is unchanged)" % type(e).__name__)
        if self._audit:
            record = {
                "customer": mask_email(email), "message_chars": len(message), "intent": res.intent,
                "order": res.order_name, "action": res.action, "escalation_reason": res.escalation_reason,
                "guard_violations": list(res.guard_violations), "steps": res.steps}
            if res.enquiry:  # counts and kinds only: no name, no email, no phone and none of the visitor's words
                e = res.enquiry
                record["enquiry"] = {"intent": e.intent, "queue": e.queue, "consent": e.consent, "captured": e.captured,
                                     "lead": e.lead_status, "vendor": e.vendor_code, "fields_sent": e.fields_sent}
            self._audit.write(record)
        return res

    # -- internals --------------------------------------------------------------------------

    def _recall(self, email: str, res: Result) -> list:
        if not self._memory:
            return []
        try:
            memories = self._memory.recall(email)
        except Exception as e:  # memory is a convenience: its outage must never block an answer
            res.steps.append("memory unavailable: %s" % type(e).__name__)
            return []
        res.steps.append("memory: %d note(s) recalled, %d stale" % (len(memories), sum(1 for m in memories if m.stale)))
        return memories

    def _remember(self, email: str, message: str, res: Result) -> None:
        """Runs only after a reply passed the guard. Escalated turns and unanswered turns store nothing."""
        if not self._memory or res.action != "replied":
            return
        items: list[Candidate] = []
        phrase = ORDER_WORDS.get(res.intent)
        if phrase and res.order_name:
            items.append(Candidate("episode", "last_" + res.intent, phrase % res.order_name, source="agent_note"))
            items.append(Candidate("live_ref", "last_order", None, source="agent_note", ref="shopify:order:" + res.order_name))
        elif res.intent == "policy_question":
            items.append(Candidate("episode", "last_policy_question", "asked a policy question", source="agent_note"))
        elif res.intent == "product_question":
            items.append(Candidate("episode", "last_product_question", "asked about a product", source="agent_note"))
        if len(message) >= 12:
            try:
                proposed = parse_json(self._llm.complete(MEMORY_EXTRACT_SYSTEM, message, max_tokens=400)).get("memories") or []
                for raw in proposed[:MAX_PER_TURN]:
                    kept = clean_candidate(raw, message)
                    if kept:
                        items.append(kept)
            except (LLMError, AttributeError, TypeError) as e:
                res.steps.append("memory: nothing extracted (%s)" % type(e).__name__)
        if not items:
            return
        try:
            res.steps.append("memory: wrote %d note(s)" % self._memory.remember(email, items))
        except Exception as e:
            res.steps.append("memory not saved: %s" % type(e).__name__)

    def _capture_lead(self, res: Result, email: str, conversation_id: str | None) -> None:
        """After the reply is decided. A lead is captured only on a turn that was answered (never an escalated one), and a
        failure is recorded on the Result and nowhere else: the visitor's reply is already final."""
        if not self._enquiry or res.action != "replied":
            return
        state = self._enquiry.ready(self._enquiry.key(conversation_id, email))
        if not state:
            return
        state.attempts += 1
        outcome = self._enquiry.capture.capture(dict(state.fields), state.intent, state.summary)
        state.captured = outcome.status != "failed"
        info = res.enquiry
        if info:
            info.lead_status, info.vendor_code, info.fields_sent = outcome.status, outcome.vendor_code, outcome.fields_sent
            info.captured = state.captured
        if outcome.status == "failed":
            res.steps.append("lead capture failed: %s %s (the reply is unchanged)" % (outcome.vendor_code, outcome.detail))
        else:
            res.steps.append("lead capture: %s" % outcome.status)

    def _escalate(self, res: Result, reason: str) -> Result:
        res.action, res.reply, res.escalation_reason = "escalated", HOLDING_REPLY, reason
        res.steps.append("escalated: " + reason)
        return res

    def _run(self, message: str, email: str, conversation_id: str | None = None) -> Result:
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

        # 2a. enquiry routing, when a desk is attached. The model proposed an intent; code checked it against the list, and the
        # owner queue and the follow-up come from the table in code. This can add an escalation (human_needed), never remove one.
        if self._enquiry:
            routing = self._enquiry.route(triage, res.intent)
            res.enquiry = EnquiryInfo(routing.intent, routing.route.queue, routing.follow_up)
            res.steps.append(routing.step)
            if routing.intent == "human_needed":
                return self._escalate(res, "enquiry routed to a person")
            key = self._enquiry.key(conversation_id, email)
            turn = self._enquiry.converse(key, message, email, triage, routing, res.steps)
            if turn:  # a lead dialogue: the reply is written in code, so it needs no facts and no guard
                res.reply = turn.reply
                lead = self._enquiry.route({"enquiry_intent": turn.intent}, res.intent)  # the conversation's intent, not this message's
                res.enquiry = EnquiryInfo(lead.intent, lead.route.queue, lead.follow_up, turn.consent, turn.consent_asked,
                                        turn.awaiting_consent)
                return res

        # 2b. what we remember about this customer: hints and one pointer, never facts
        memories = self._recall(email, res)

        # 3. facts
        facts: list[str] = []
        try:
            if res.intent in ("order_status", "return_request"):
                number = re.sub(r"\D", "", str(triage.get("order_number") or ""))
                from_memory = False
                if not number:
                    number = last_order_number(memories) or ""
                    from_memory = bool(number)
                    if from_memory:
                        res.steps.append("order number taken from the memory pointer, to be verified live")
                if not number:
                    res.reply = NEEDS_ORDER_REPLY
                    res.steps.append("asked for the order number")
                    return res
                order = self._shop.find_order(number, email)
                if order is None and from_memory:
                    res.reply = NEEDS_ORDER_REPLY
                    res.steps.append("remembered order not found for this email: asked for the number instead")
                    return res
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
        notes = format_hints(memories)
        notes_block = ("\n\n" + notes) if notes else ""
        allowed_extra = frozenset(figures(message))
        feedback = ""
        for attempt in (1, 2):
            try:
                reply = self._llm.complete(
                    ANSWER_SYSTEM, "FACTS:\n%s%s\n\nCUSTOMER MESSAGE:\n%s%s" % (facts_text, notes_block, message, feedback),
                    max_tokens=1500)
            except LLMError as e:
                res.steps.append("answer failed: %s" % e)
                return self._escalate(res, "answer generation failed")
            check = verify_grounded(reply, facts_text, allowed_extra=allowed_extra)
            if check.ok:
                res.reply = reply
                res.steps.append("guard: passed on attempt %d" % attempt)
                self._remember(email, message, res)
                return res
            res.guard_violations = check.violations
            res.steps.append("guard: refused attempt %d (%s)" % (attempt, "; ".join(check.violations)))
            feedback = "\n\nYour last reply was refused: %s. Rewrite it using only the FACTS." % "; ".join(check.violations)
        return self._escalate(res, "reply failed the grounding check twice")
