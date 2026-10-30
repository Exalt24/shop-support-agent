"""Enquiry routing and the rules for a lead: what a visitor may be asked, what may be kept, who may be told.

The order is the design, same as core.py:
  1. the escalation floor runs first (core.py), so a sales enquiry that is also a legal threat is a person's, not a lead;
  2. the model only PROPOSES an intent from INTENTS; code checks the proposal, and a value that is not on the list is
     `human_needed`. The model can add an escalation, never remove one;
  3. a routing table in code decides the owner queue and the follow-up; the model has no say in either;
  4. a lead is built only from what the visitor wrote, after the visitor said an explicit yes to a question the agent asked
     in an EARLIER turn. The model never decides consent. Nothing is captured on an escalated turn.

Every reply on a lead turn is written here, in code. The model does not word it, so there is no figure or promise in it for a
guard to chase.
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import date, timedelta

from .memory import CARD_RE, SECRET_RE

INTENTS = ("order_support", "product_question", "sales_enquiry", "quote_request", "service_request", "general", "human_needed")
LEAD_INTENTS = frozenset({"sales_enquiry", "quote_request", "service_request"})
# What the existing triage already calls things, so a model that proposes nothing new still routes.
LEGACY_INTENT = {"order_status": "order_support", "return_request": "order_support", "policy_question": "general",
                 "product_question": "product_question", "other": "general"}


@dataclass(frozen=True)
class Route:
    queue: str
    task_subject: str | None = None  # None: no follow-up task (the agent or a person already handles it)
    due_days: int = 0                # business days
    priority: str = "Normal"         # one of Zoho's documented task priorities


ROUTES: dict[str, Route] = {
    "order_support": Route("support"),
    "product_question": Route("support"),
    "general": Route("support"),
    "human_needed": Route("support", "Take over this chat", 0, "High"),
    "sales_enquiry": Route("sales", "Follow up: new sales enquiry from web chat", 1, "Normal"),
    "quote_request": Route("sales", "Prepare a quote: request from web chat", 1, "High"),
    "service_request": Route("service", "Book a service visit: request from web chat", 2, "Normal"),
}
assert set(ROUTES) == set(INTENTS)

TEAM_WORD = {"sales": "sales", "service": "service", "support": "support"}


def team_word(queue: str) -> str:
    """How the visitor hears a queue name: the three default queues by their word, a tenant's own queue by its name."""
    return TEAM_WORD.get(queue) or queue.replace("-", " ")


def routes_with_queues(queues: dict[str, str]) -> dict[str, Route]:
    """A tenant's routing table: the default routes with the owner queue of some intents replaced. Only the queue changes. The
    follow-up subject, due days and priority stay the ones above, and an intent that is not listed keeps its default queue."""
    unknown = set(queues) - set(ROUTES)
    if unknown:
        raise ValueError("not an intent: " + ", ".join(sorted(unknown)))
    return {i: (replace(r, queue=queues[i]) if i in queues else r) for i, r in ROUTES.items()}


@dataclass(frozen=True)
class Routing:
    intent: str
    route: Route
    step: str

    @property
    def follow_up(self) -> str:
        r = self.route
        if not r.task_subject:
            return "none"
        return "%s task due in %d business day%s" % (r.queue, r.due_days, "" if r.due_days == 1 else "s")


def route_enquiry(triage: dict, legacy_intent: str, routes: dict[str, Route] = ROUTES) -> Routing:
    """Validate the model's proposal against INTENTS. No proposal: fall back to the old triage intent. A proposal that is
    not on the list: human_needed, because a value nobody planned for is not something to route on a guess."""
    proposed = triage.get("enquiry_intent")
    if proposed is None:
        intent, step = LEGACY_INTENT.get(legacy_intent, "general"), "routing: no proposal, taken from triage intent %s" % legacy_intent
    elif isinstance(proposed, str) and proposed.strip().lower() in INTENTS:
        intent, step = proposed.strip().lower(), "routing: model proposed a listed intent"
    else:
        intent, step = "human_needed", "routing: the model proposed a value that is not on the list, treated as human_needed"
    return Routing(intent, routes[intent], step + ", queue=%s" % routes[intent].queue)


def add_business_days(start: date, days: int) -> date:
    d = start
    while days > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days -= 1
    return d


# -- consent: an explicit yes in the visitor's own words, never the model's say-so ---------------------------------------

YES_RE = re.compile(r"^\W*(?:yes|yep|yeah|yup|sure|ok|okay|please do|go ahead|of course|that'?s fine|that is fine|i agree|i consent|you may|you can)\b", re.I)
NEG_RE = re.compile(r"\b(?:no|nope|nah|not|don'?t|do not|never|stop|without|rather not|can'?t|cannot|won'?t)\b", re.I)
NO_RE = re.compile(r"^\W*(?:no|nope|nah|not|don'?t|do not|never|stop)\b", re.I)


def is_explicit_yes(message: str) -> bool:
    """A yes at the start, and nothing in the message that takes it back ("yes but don't call me" is not a yes)."""
    return bool(YES_RE.match(message)) and not NEG_RE.search(message)


def is_explicit_no(message: str) -> bool:
    return bool(NO_RE.match(message))


# -- fields: the model proposes, code validates, and every value must be in what the visitor wrote --------------------------

EMAIL_IN_TEXT = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")
EMAIL_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._%+\-]{0,63}@[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?"
                      r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)*\.[A-Za-z]{2,24}$")
PHONE_IN_TEXT = re.compile(r"(?<![\w@.])\+?\(?\d[\d\s().\-]{5,22}\d(?![\w@])")
NAME_INTRO = re.compile(r"\bmy name is\s+([^\W\d_][^\W\d_'’.\-]*(?:\s+[^\W\d_][^\W\d_'’.\-]*){0,3})", re.I)
NOT_NAMES = frozenset({"yes", "no", "hi", "hello", "hey", "sir", "madam", "ok", "okay", "thanks", "none", "null", "unknown", "customer"})
MIN_PHONE_DIGITS, MAX_PHONE_DIGITS = 9, 12  # 13 digits and up is a possible card number (memory.CARD_RE), so it is refused


def valid_email(value: str) -> bool:
    return bool(EMAIL_OK.match(value)) and len(value) <= 254 and ".." not in value


def valid_phone(value: str) -> str | None:
    """The phone with formatting removed (a leading + is kept), or None when it is not a phone number."""
    digits = re.sub(r"\D", "", value)
    if not (MIN_PHONE_DIGITS <= len(digits) <= MAX_PHONE_DIGITS):
        return None
    return ("+" if value.strip().lstrip("(").startswith("+") else "") + digits


def _words(text: str) -> set[str]:
    return {w.lower() for w in re.findall(r"[^\W\d_]+", text)}


def valid_name(value: object, said: str) -> str | None:
    if not isinstance(value, str):
        return None
    name = " ".join(value.split())
    if not (2 <= len(name) <= 60) or len(name.split()) > 4 or not name[0].isalpha():
        return None
    if not all(c.isalpha() or c in " '’.-" for c in name):
        return None
    if SECRET_RE.search(name) or {w.lower() for w in name.split()} & NOT_NAMES:
        return None
    # every word must be one the visitor wrote: the model cannot hand us a name it made up
    if not _words(name) <= _words(said):
        return None
    return name


def valid_company(value: object, said: str) -> str | None:
    if not isinstance(value, str):
        return None
    company = " ".join(value.split())
    if not (2 <= len(company) <= 80) or not all(c.isalnum() or c in " &.,'’-" for c in company):
        return None
    if SECRET_RE.search(company) or CARD_RE.search(company):
        return None
    if not {w.lower() for w in re.findall(r"[^\W_]+", company)} <= {w.lower() for w in re.findall(r"[^\W_]+", said)}:
        return None  # a company the visitor never named is never invented
    return company


def read_fields(lead: object, message: str, session_email: str) -> tuple[dict[str, str], list[str]]:
    """Name, email, phone and company from ONE visitor message. `lead` is the model's proposal. The visitor's own email
    (the one the chat was opened with) counts as given. Returns the values that passed and short notes (no values) on what
    was refused."""
    proposal = lead if isinstance(lead, dict) else {}
    said = message
    out: dict[str, str] = {}
    notes: list[str] = []
    if CARD_RE.search(message):
        notes.append("a card-like number in the message was ignored: never read as a phone number, never copied anywhere")

    emails = {e.lower(): e for e in EMAIL_IN_TEXT.findall(said) if valid_email(e)}
    if session_email and valid_email(session_email.strip()):
        emails.setdefault(session_email.strip().lower(), session_email.strip())
    p_email = proposal.get("email")
    if isinstance(p_email, str) and p_email.strip() and p_email.strip().lower() not in emails:
        notes.append("model proposed an email that is not in what the visitor wrote: refused")
    if len(emails) == 1:
        out["email"] = next(iter(emails.values()))
    elif len(emails) > 1:
        notes.append("more than one email in play: none used, so another person's address is never taken")

    phones = {}
    for m in PHONE_IN_TEXT.finditer(said):
        v = valid_phone(m.group(0))
        if v:
            phones[re.sub(r"\D", "", v)] = v
    p_phone = proposal.get("phone")
    if isinstance(p_phone, str) and p_phone.strip():
        v = valid_phone(p_phone)
        if not v or re.sub(r"\D", "", v) not in phones:
            notes.append("model proposed a phone number that is not a valid one in what the visitor wrote: refused")
    if len(phones) == 1:
        out["phone"] = next(iter(phones.values()))
    elif len(phones) > 1:
        notes.append("more than one phone number in play: none used")

    name = valid_name(proposal.get("name"), said)
    if not name:
        m = NAME_INTRO.search(said)
        name = valid_name(m.group(1), said) if m else None
    if name:
        out["name"] = name
    elif isinstance(proposal.get("name"), str) and proposal["name"].strip():
        notes.append("model proposed a name that failed the checks: refused")
    company = valid_company(proposal.get("company"), said)
    if company:
        out["company"] = company
    elif isinstance(proposal.get("company"), str) and proposal["company"].strip():
        notes.append("model proposed a company the visitor did not name: refused")
    return out, notes


def split_name(name: str) -> tuple[str | None, str]:
    """(First_Name, Last_Name). Zoho's Leads need Last_Name, so a single word is the last name."""
    parts = name.split()
    return (" ".join(parts[:-1]) or None), parts[-1]


# -- the note text: built by code from the visitor's own words ----------------------------------------------------------------

SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
SUMMARY_LIMIT = 280


def summarise(message: str, limit: int = SUMMARY_LIMIT) -> str:
    """The visitor's words, shortened, with cards, emails, phone numbers and any sentence that mentions a secret removed.
    The contact details travel as lead fields; they are not repeated in the note."""
    kept = [s for s in SENTENCE_RE.split(" ".join(message.split())) if s and not SECRET_RE.search(s)]
    text = " ".join(kept)
    text = CARD_RE.sub("[number removed]", text)
    text = EMAIL_IN_TEXT.sub("[email]", text)
    text = PHONE_IN_TEXT.sub(lambda m: "[phone]" if valid_phone(m.group(0)) else m.group(0), text)
    if len(text) > limit:
        text = text[: limit - 3].rsplit(" ", 1)[0].rstrip(",;:") + "..."
    return text or "(no text kept)"


# -- per conversation state ---------------------------------------------------------------------------------------------------

@dataclass
class EnquiryState:
    intent: str | None = None
    fields: dict[str, str] = field(default_factory=dict)
    summary: str = ""
    consent_asked: bool = False
    consent: bool | None = None
    session_email_read: bool = False  # the chat's own email is a candidate once; a conflict seen then is not undone by a later turn
    confirmed: bool = False
    captured: bool = False
    attempts: int = 0
    touched: float = 0.0


def conversation_key(tenant: str, conversation_id: str | None, email: str) -> str:
    who = conversation_id or "email:" + hashlib.sha256(email.strip().lower().encode()).hexdigest()[:16]
    return "%s|%s" % (tenant, who)


class EnquiryTracker:
    """In memory, one process. An hour's silence drops a conversation, and so does the 2000th, so it cannot grow without bound."""
    TTL_SECONDS, MAX_CONVERSATIONS = 3600, 2000

    def __init__(self, now=time.time):
        self._now, self._items, self._lock = now, {}, threading.Lock()

    def get(self, key: str) -> EnquiryState:
        with self._lock:
            t = self._now()
            for k in [k for k, s in self._items.items() if t - s.touched > self.TTL_SECONDS]:
                del self._items[k]
            if key not in self._items and len(self._items) >= self.MAX_CONVERSATIONS:
                del self._items[min(self._items, key=lambda k: self._items[k].touched)]
            st = self._items.setdefault(key, EnquiryState())
            st.touched = t
            return st


@dataclass
class EnquiryInfo:
    """What one turn decided, for the Result and the audit. Counts and kinds, never the visitor's words."""
    intent: str
    queue: str
    follow_up: str
    consent: bool = False
    consent_asked: bool = False
    awaiting_consent: bool = False  # the question is out and not answered yet: a chat widget shows its yes and no buttons
    captured: bool = False
    lead_status: str = "none"
    vendor_code: str | None = None
    fields_sent: int = 0


@dataclass(frozen=True)
class Turn:
    reply: str
    intent: str  # the lead intent the conversation is about, which may differ from this one message's ("yes" is "general")
    consent: bool
    consent_asked: bool
    awaiting_consent: bool = False


# -- the words said to the visitor ---------------------------------------------------------------------------------------------

def _first(state: EnquiryState) -> str:
    name = state.fields.get("name")
    return name.split()[0] if name else "there"


def ask_for_details(state: EnquiryState, team: str) -> str:
    has_name, has_contact = "name" in state.fields, "email" in state.fields or "phone" in state.fields
    if not has_name and not has_contact:
        return ("Happy to help with that. So our %s team can get back to you, could you tell me your name and an email address "
                "or phone number?" % team)
    if not has_name:
        return "Thanks. What name should our %s team use when they get in touch?" % team
    return "Thanks, %s. What email address or phone number should our %s team use?" % (_first(state), team)


def ask_consent(state: EnquiryState, team: str) -> str:
    return ("Thanks, %s. May I pass your name, your contact details and a short note of what you asked to our %s team so they "
            "can follow up? Please reply yes or no." % (_first(state), team))


def confirmed(state: EnquiryState, team: str) -> str:
    return ("Thanks, %s. I've noted your request for our %s team, and they will follow up with you using the details you gave."
            % (_first(state), team))


def already_confirmed(team: str) -> str:
    return "Our %s team has your request and will be in touch. Is there anything else I can help with?" % team


def declined(team: str) -> str:
    return "No problem, I won't pass your details to our %s team." % team


def unclear(team: str) -> str:
    return "I haven't passed your details on. If you'd like our %s team to follow up with you, please reply yes." % team


class EnquiryDesk:
    """The conversation side of an enquiry: classification lives in route_enquiry, this holds the lead dialogue. The capture
    itself (agent/leads.py) is a separate object so it can be tested, and fail, on its own."""

    def __init__(self, capture, *, tenant: str = "default", tracker: EnquiryTracker | None = None,
                 routes: dict[str, Route] | None = None):
        self.capture, self.tenant, self.tracker = capture, tenant, tracker or EnquiryTracker()
        self.routes = routes or ROUTES  # a tenant can send the same intent to its own owner queue
        self.max_attempts = 3

    def key(self, conversation_id: str | None, email: str) -> str:
        return conversation_key(self.tenant, conversation_id, email)

    def route(self, triage: dict, legacy_intent: str) -> Routing:
        return route_enquiry(triage, legacy_intent, self.routes)

    def converse(self, key: str, message: str, email: str, triage: dict, routing: Routing, steps: list[str]) -> Turn | None:
        """The reply for a lead turn, or None when this turn is not part of a lead conversation (the normal flow answers)."""
        state = self.tracker.get(key)
        fresh = routing.intent in LEAD_INTENTS
        if fresh:
            new_enquiry = state.intent != routing.intent or state.captured or not state.summary
            if state.intent is None:  # the last dialogue ended (they said no): consent starts over for a new enquiry
                state.consent, state.consent_asked = None, False
            if new_enquiry:
                state.captured, state.confirmed, state.summary = False, False, summarise(message)
            state.intent = routing.intent
        elif not (state.intent in LEAD_INTENTS and routing.intent == "general"):
            return None  # an order or product question mid-conversation is answered normally; the lead state waits
        intent = state.intent
        team = team_word(self.routes[intent].queue)

        # consent counts only if the question was asked in an EARLIER turn; a yes volunteered before it is not an answer to it
        asked_before = state.consent_asked
        if asked_before and state.consent is not True:
            if is_explicit_yes(message):
                state.consent = True
            elif is_explicit_no(message):
                state.consent = False
        found, notes = read_fields(triage.get("lead"), message, "" if state.session_email_read else email)
        state.session_email_read = True
        steps.extend("lead: " + n for n in notes)
        state.fields.update(found)

        complete = "name" in state.fields and ("email" in state.fields or "phone" in state.fields)
        if state.consent is False:
            reply = declined(team)
            state.intent = None  # the dialogue is over unless they start a new enquiry
        elif not complete:
            reply = ask_for_details(state, team)
        elif not state.consent_asked:
            state.consent_asked = True
            reply = ask_consent(state, team)
        elif state.consent is True:
            reply = already_confirmed(team) if state.confirmed else confirmed(state, team)
            state.confirmed = True
        else:
            reply = unclear(team)
        steps.append("lead: %s, consent %s" % ("details complete" if complete else "details missing",
                                               {True: "given", False: "refused", None: "not given"}[state.consent]))
        return Turn(reply, intent, consent=state.consent is True, consent_asked=state.consent_asked,
                    awaiting_consent=state.consent_asked and state.consent is None)

    def ready(self, key: str) -> EnquiryState | None:
        """The state if a lead may be captured now: a lead intent, a name and a contact, an explicit yes, not captured yet."""
        state = self.tracker.get(key)
        if (state.intent in LEAD_INTENTS and state.consent is True and not state.captured and state.attempts < self.max_attempts
                and "name" in state.fields and ("email" in state.fields or "phone" in state.fields)):
            return state
        return None
