"""Enquiry routing and lead capture, end to end through SupportAgent: a scripted model, the fake shop, the FAKE Zoho.

Nothing here touches a real Zoho or Shopify account. The model is a script, so these tests say what the CODE does with
whatever the model proposes, which is the point: the code is the guard.
"""
import json
from datetime import date

import pytest

from agent.audit import AuditLog
from agent.core import SupportAgent, TRIAGE_SYSTEM
from agent.enquiry import (INTENTS, LEAD_INTENTS, ROUTES, EnquiryDesk, add_business_days, is_explicit_no, is_explicit_yes, read_fields,
                           route_enquiry, summarise, valid_email, valid_phone)
from agent.escalation import HOLDING_REPLY
from agent.leads import LeadCapture, LeadLedger, idempotency_key
from agent.llm import LLMError
from agent.zoho_crm import ZohoCrmClient, ZohoSettings
from agent.zoho_fake import FakeZoho

from conftest import FakeShop, order

FRIDAY = date(2026, 10, 30)
JANE = "jane.doe@example.com"
CALM = {"angry": False, "legal_threat": False, "chargeback": False, "wants_human": False}


def tri(intent="sales_enquiry", lead=None, legacy="other", **extra):
    return {"intent": legacy, "order_number": None, "product_query": None, "enquiry_intent": intent, "lead": lead or {}, **CALM, **extra}


class TurnLLM:
    """One triage JSON per turn, in order. Any other prompt (an answer) takes the next scripted answer."""

    def __init__(self, triages, answers=()):
        self.triages, self.answers, self.calls = list(triages), list(answers), []

    def complete(self, system, user, *, max_tokens=1800):
        self.calls.append((system, user))
        if system == TRIAGE_SYSTEM:
            if not self.triages:
                raise LLMError("no scripted triage left")
            return json.dumps(self.triages.pop(0))
        if not self.answers:
            raise LLMError("no scripted answer left")
        return self.answers.pop(0)


class Rig:
    def __init__(self, region="us", tenant="shop-a", day=FRIDAY, rules=None, audit=None):
        self.http = FakeZoho(region)
        self.crm = ZohoCrmClient(ZohoSettings("cid", "csecret", "rtoken", region), self.http, sleep=lambda s: None)
        self.day = day
        self.capture = LeadCapture(self.crm, tenant=tenant, today=lambda: self.day, assignment_rules=rules)
        self.desk = EnquiryDesk(self.capture, tenant=tenant)
        self.audit = audit

    def agent(self, triages, answers=(), shop=None):
        self.llm = TurnLLM(triages, answers)
        return SupportAgent(self.llm, shop or FakeShop(orders={"1019": (JANE, order())}), [], self.audit, None, self.desk)


def chat(agent, messages, email=JANE, conv="c1"):
    return [agent.handle(m, email, conv) for m in messages]


FULL = {"name": "Jane Doe", "email": JANE}
QUOTE_MSG = "Hi, I'd like a quote for fitting out 20 desks. My name is Jane Doe and my email is jane.doe@example.com"


def happy(rig, conv="c1", email=JANE, intent="quote_request"):
    """Turn 1: the enquiry with details (agent asks for consent). Turn 2: an explicit yes (agent captures)."""
    a = rig.agent([tri(intent, FULL), tri("general")])
    return a, chat(a, [QUOTE_MSG, "Yes please, go ahead."], email, conv)


# ---- classification: the model proposes, code decides -------------------------------------------------------------------

@pytest.mark.parametrize("intent,queue", [("order_support", "support"), ("product_question", "support"), ("general", "support"),
                                          ("sales_enquiry", "sales"), ("quote_request", "sales"), ("service_request", "service"),
                                          ("human_needed", "support")])
def test_each_intent_is_routed_to_its_owner_queue(intent, queue):
    assert route_enquiry({"enquiry_intent": intent}, "other").route.queue == queue


def test_every_listed_intent_has_a_route_and_only_lead_intents_get_a_follow_up_task_besides_human_needed():
    assert set(ROUTES) == set(INTENTS)
    assert {i for i, r in ROUTES.items() if r.task_subject} == LEAD_INTENTS | {"human_needed"}


def test_a_model_that_proposes_nothing_falls_back_to_the_old_triage_intent():
    assert route_enquiry({}, "order_status").intent == "order_support"
    assert route_enquiry({"enquiry_intent": None}, "policy_question").intent == "general"
    assert route_enquiry({}, "something_new").intent == "general"


@pytest.mark.parametrize("bad", ["refund_everything", "", 7, ["sales_enquiry"], {"a": 1}, "sales enquiry please"])
def test_an_unknown_value_becomes_human_needed(bad):
    r = route_enquiry({"enquiry_intent": bad}, "other")
    assert r.intent == "human_needed" and "not on the list" in r.step


def test_an_unknown_value_ends_in_a_person_and_nothing_goes_to_the_crm():
    rig = Rig()
    res = rig.agent([tri("make_me_rich", FULL)]).handle(QUOTE_MSG, JANE, "c1")
    assert res.action == "escalated" and res.escalation_reason == "enquiry routed to a person" and res.reply == HOLDING_REPLY
    assert rig.http.calls == []


def test_the_model_can_add_an_escalation_with_human_needed():
    rig = Rig()
    res = rig.agent([tri("human_needed", FULL)]).handle("Can someone help with a very odd request", JANE, "c1")
    assert res.action == "escalated" and res.enquiry.intent == "human_needed"


def test_the_model_flags_still_escalate_a_sales_enquiry_and_nothing_is_captured():
    rig = Rig()
    res = rig.agent([tri("sales_enquiry", FULL, angry=True)]).handle(QUOTE_MSG, JANE, "c1")
    assert res.action == "escalated" and res.escalation_reason == "model flagged: angry"
    assert rig.http.calls == []


def test_the_escalation_floor_beats_a_sales_intent_and_the_model_never_sees_the_message():
    rig = Rig()
    a = rig.agent([tri("sales_enquiry", FULL)])
    res = a.handle("I want a quote for 20 desks or my lawyer will be in touch. Jane Doe jane.doe@example.com", JANE, "c1")
    assert res.action == "escalated" and res.escalation_reason == "legal" and rig.llm.calls == [] and rig.http.calls == []
    assert res.enquiry is None  # routing never ran


def test_without_a_desk_the_agent_is_exactly_what_it_was():
    llm = TurnLLM([tri("sales_enquiry", FULL)])
    res = SupportAgent(llm, FakeShop(), [], None).handle(QUOTE_MSG, JANE)
    assert res.enquiry is None and res.action == "escalated" and res.escalation_reason == "out of scope"


def test_order_and_product_enquiries_are_still_answered_from_the_facts():
    rig = Rig()
    good = "Your order #1019 is paid and hasn't shipped yet. It was placed 2026-09-20 for 250.0 USD."
    t = dict(tri("order_support", legacy="order_status"), order_number="1019")
    res = rig.agent([t], [good]).handle("Where is order 1019?", JANE, "c1")
    assert res.action == "replied" and res.reply == good and res.enquiry.queue == "support" and res.enquiry.follow_up == "none"
    assert rig.http.calls == []


# ---- consent ------------------------------------------------------------------------------------------------------------

def test_the_agent_asks_once_in_plain_words_and_captures_nothing_yet():
    rig = Rig()
    a = rig.agent([tri("quote_request", FULL), tri("general")])
    first, second = chat(a, [QUOTE_MSG, "What are your opening hours, by the way?"])
    assert "May I pass your name" in first.reply and "reply yes or no" in first.reply and first.enquiry.consent_asked
    assert "May I pass" not in second.reply  # asked once
    assert rig.http.calls == [] and not second.enquiry.captured


def test_a_yes_volunteered_before_the_question_is_not_an_answer_to_it():
    rig = Rig()
    a = rig.agent([tri("quote_request", FULL)])
    res = a.handle("Yes, please! " + QUOTE_MSG, JANE, "c1")  # opens with a yes, as a visitor answering nothing might
    assert "May I pass your name" in res.reply and not res.enquiry.consent
    assert rig.http.calls == []


def test_a_model_that_claims_consent_does_not_create_it():
    rig = Rig()
    a = rig.agent([tri("quote_request", FULL, consent=True), tri("general", consent=True, lead={"consent": True})])
    r1, r2 = chat(a, [QUOTE_MSG, "Tell me more about your fitting service"])
    assert not r2.enquiry.consent and rig.http.calls == []


@pytest.mark.parametrize("text,yes", [("Yes please", True), ("yes, go ahead", True), ("Sure", True), ("OK", True), ("Of course.", True),
                                      ("yes but don't call me", False), ("No", False), ("no thanks", False), ("not yet", False),
                                      ("what is the price?", False), ("", False), ("I said yes to the other thing but not this", False)])
def test_only_an_explicit_yes_with_nothing_taking_it_back_counts(text, yes):
    assert is_explicit_yes(text) is yes


def test_a_no_is_recorded_and_nothing_is_passed_on():
    rig = Rig()
    a = rig.agent([tri("quote_request", FULL), tri("general")])
    r1, r2 = chat(a, [QUOTE_MSG, "No thanks"])
    assert is_explicit_no("No thanks") and "won't pass your details" in r2.reply and not r2.enquiry.consent
    assert rig.http.calls == []


def test_a_yes_that_takes_itself_back_is_not_consent():
    rig = Rig()
    a = rig.agent([tri("quote_request", FULL), tri("general")])
    _, r2 = chat(a, [QUOTE_MSG, "yes but don't share my phone"])
    assert not r2.enquiry.consent and "haven't passed your details on" in r2.reply and rig.http.calls == []


def test_missing_details_are_asked_for_before_consent_is_asked():
    rig = Rig()
    a = rig.agent([tri("service_request"), tri("general", {"name": "Jane Doe"})])
    r1, r2 = chat(a, ["Our printer needs servicing", "I'm Jane Doe, my name is Jane Doe"], email="")
    assert "name and an email address or phone number" in r1.reply and not r1.enquiry.consent_asked
    assert "What email address or phone number" in r2.reply and not r2.enquiry.consent_asked


# ---- capture ------------------------------------------------------------------------------------------------------------

def test_with_consent_the_lead_task_and_note_reach_the_crm_and_the_reply_confirms():
    rig = Rig(rules={"sales": "rule-sales"})
    a, (r1, r2) = happy(rig)
    http = rig.http
    assert r2.enquiry.captured and r2.enquiry.lead_status == "created" and r2.enquiry.vendor_code == "SUCCESS"
    assert "Thanks, Jane." in r2.reply and "sales team" in r2.reply
    (lead_id, lead), = http.leads.items()
    assert lead == {"Last_Name": "Doe", "First_Name": "Jane", "Email": JANE}  # nothing else: no company was named
    assert http.api_calls()[0][2] == {"data": [lead], "duplicate_check_fields": ["Email"]}
    task, = http.tasks
    assert task["Subject"] == "Prepare a quote: request from web chat" and task["Priority"] == "High"
    assert task["Due_Date"] == "2026-11-02"  # one business day after Friday is Monday
    assert task["What_Id"] == {"id": lead_id} and task["$se_module"] == "Leads" and task["Status"] == "Not Started"
    assert task["_lar_id"] == "rule-sales"
    note, = http.notes
    assert note["lead_id"] == lead_id and "fitting out 20 desks" in note["content"] and "quote request" in note["content"]
    assert JANE not in note["content"]  # the contact is on the lead, not repeated in the note


def test_a_service_request_goes_to_the_service_queue_with_a_two_day_follow_up():
    rig = Rig()
    a = rig.agent([tri("service_request", {"name": "Sam Lee", "phone": "0917 123 4567"}), tri("general")])
    r1, r2 = chat(a, ["The compressor at our site is down, can you send someone? I'm Sam Lee, call me on 0917 123 4567"], email="") + \
        chat(a, ["yes"], email="")
    assert r2.enquiry.queue == "service" and "service team" in r2.reply
    lead, = rig.http.leads.values()
    assert lead == {"Last_Name": "Lee", "First_Name": "Sam", "Phone": "09171234567"}
    assert "Email" not in lead
    assert rig.http.tasks[0]["Due_Date"] == "2026-11-03"
    body = rig.http.api_calls()[0][2]
    assert "duplicate_check_fields" not in body  # no email, nothing for Zoho to check


def test_a_lead_is_built_from_the_visitors_words_only_so_a_made_up_name_company_or_phone_is_refused():
    rig = Rig()
    lead = {"name": "Alexander Hamilton", "company": "Acme Industries", "email": "ceo@bigcorp.com", "phone": "+14155550132"}
    a = rig.agent([tri("sales_enquiry", lead), tri("general")])
    first, = chat(a, ["I'd like to talk about pricing, my name is Jane Doe"], email=JANE)
    chat(a, ["yes"])
    lead_sent, = rig.http.leads.values()
    assert lead_sent == {"Last_Name": "Doe", "First_Name": "Jane", "Email": JANE}
    steps = " ".join(first.steps)
    assert "email that is not in what the visitor wrote" in steps and "company the visitor did not name" in steps


def test_a_company_the_visitor_named_is_kept():
    rig = Rig()
    a = rig.agent([tri("sales_enquiry", {"name": "Jane Doe", "company": "Acme Industries"}), tri("general")])
    chat(a, ["Jane Doe from Acme Industries, interested in your plans"])
    chat(a, ["yes"])
    assert list(rig.http.leads.values())[0]["Company"] == "Acme Industries"


def test_no_data_from_the_shop_or_another_customer_reaches_the_lead():
    rig = Rig()
    shop = FakeShop(orders={"1019": ("someone.else@example.com", order())})
    a = rig.agent([tri("sales_enquiry", FULL), tri("general")], shop=shop)
    chat(a, ["Pricing for bulk orders please, I'm Jane Doe, jane.doe@example.com. My colleague Bob (bob@example.com) asked too."])
    res, = chat(a, ["yes"])
    assert res.enquiry.captured is False  # two emails in play: none used, no name+contact pair
    assert rig.http.calls == []
    dump = json.dumps([rig.http.calls, rig.http.leads, rig.http.notes])
    assert "someone.else" not in dump and "bob@" not in dump and "USB Audio" not in dump


def test_when_the_visitor_gives_a_phone_the_second_person_email_does_not_block_it():
    rig = Rig()
    a = rig.agent([tri("sales_enquiry", FULL), tri("general")])
    chat(a, ["Pricing please. I'm Jane Doe, 0917 123 4567. Bob is bob@example.com and I am jane.doe@example.com"])
    chat(a, ["yes"])
    lead, = rig.http.leads.values()
    assert lead == {"Last_Name": "Doe", "First_Name": "Jane", "Phone": "09171234567"}


# ---- invalid input -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["jane@localhost", "jane@@example.com", "jane example.com", "@example.com", "jane@example", "a..b@example.com", ""])
def test_an_invalid_email_is_refused(bad):
    assert not valid_email(bad)


def test_an_invalid_session_email_is_not_used_and_the_agent_asks_for_a_contact():
    rig = Rig()
    a = rig.agent([tri("sales_enquiry", {"name": "Jane Doe"})])
    res = a.handle("Pricing please, I'm Jane Doe", "jane@localhost", "c1")
    assert "What email address or phone number" in res.reply and not res.enquiry.consent_asked
    assert rig.http.calls == []


@pytest.mark.parametrize("text,expect", [("0917 123 4567", "09171234567"), ("+63 965 935 1300", "+639659351300"), ("(415) 555-0132", "4155550132"),
                                         ("12345", None), ("4111 1111 1111 1111", None), ("4111111111111111", None), ("abc", None)])
def test_phone_shapes(text, expect):
    assert valid_phone(text) == expect


def test_a_card_like_number_is_stripped_everywhere_and_never_taken_for_a_phone():
    rig = Rig()
    msg = "Pricing please. I'm Jane Doe, my card is 4111 1111 1111 1111 if you need it, jane.doe@example.com"
    a = rig.agent([tri("sales_enquiry", {"name": "Jane Doe", "phone": "4111111111111111"}), tri("general")])
    r1 = a.handle(msg, JANE, "c1")
    res, = chat(a, ["yes"])
    dump = json.dumps([rig.http.calls, rig.http.leads, rig.http.notes])
    assert "4111" not in dump and "Phone" not in list(rig.http.leads.values())[0]
    assert any("card-like number" in s for s in r1.steps) and any("phone number that is not a valid one" in s for s in r1.steps)
    assert "4111" not in json.dumps(res.steps) and "4111" not in r1.reply


def test_a_sentence_with_a_secret_word_never_reaches_the_note():
    text = summarise("I want a quote for 5 desks. My password is hunter2 and my pin is 1234. Thanks. Mail jane.doe@example.com or call 0917 123 4567")
    assert "hunter2" not in text and "1234" not in text and "password" not in text
    assert "jane.doe" not in text and "0917" not in text and "5 desks" in text


def test_the_summary_is_truncated_at_a_word():
    text = summarise("word " * 200)
    assert len(text) <= 280 and text.endswith("...") and not text.endswith("wor...")


def test_business_days_skip_the_weekend():
    assert add_business_days(FRIDAY, 1) == date(2026, 11, 2) and add_business_days(FRIDAY, 2) == date(2026, 11, 3)
    assert add_business_days(FRIDAY, 0) == FRIDAY


# ---- duplicates and idempotency -----------------------------------------------------------------------------------------------

def test_a_repeat_enquiry_from_the_same_email_adds_a_note_not_a_second_lead_or_task():
    rig = Rig()
    happy(rig, conv="c1")
    a2 = rig.agent([tri("sales_enquiry", FULL), tri("general")])
    r1, r2 = chat(a2, ["Hello again, I'm Jane Doe (jane.doe@example.com) and I also want to know about volume pricing"] + ["yes"], conv="c2")
    assert r2.enquiry.lead_status == "noted" and r2.enquiry.captured
    assert len(rig.http.leads) == 1 and len(rig.http.tasks) == 1 and len(rig.http.notes) == 2
    assert "volume pricing" in rig.http.notes[1]["content"]
    upserts = [c for c in rig.http.api_calls() if c[1].endswith("/upsert")]
    assert len(upserts) == 1  # the second enquiry never reached the upsert


def test_the_email_is_matched_without_regard_to_case():
    rig = Rig()
    happy(rig, conv="c1")
    a2 = rig.agent([tri("quote_request", {"name": "Jane Doe"}), tri("general")])
    chat(a2, ["Also a quote for chairs, Jane Doe"] + ["ok"], email="JANE.DOE@EXAMPLE.COM", conv="c2")
    assert len(rig.http.leads) == 1 and len(rig.http.tasks) == 1


def test_the_next_day_the_same_person_is_updated_not_duplicated_and_gets_a_new_task():
    rig = Rig()
    happy(rig, conv="c1")
    rig.day = date(2026, 11, 2)
    a2 = rig.agent([tri("sales_enquiry", FULL), tri("general")])
    r = chat(a2, ["Back again, Jane Doe jane.doe@example.com, wondering about delivery times for the desks"] + ["yes"], conv="c2")[-1]
    assert r.enquiry.lead_status == "updated" and len(rig.http.leads) == 1 and len(rig.http.tasks) == 2


def test_a_retried_request_does_not_create_twice():
    rig = Rig()
    fields = {"name": "Jane Doe", "email": JANE}
    outcomes = [rig.capture.capture(fields, "quote_request", "20 desks") for _ in range(3)]
    assert [o.status for o in outcomes] == ["created", "duplicate", "duplicate"]
    assert len(rig.http.leads) == 1 and len(rig.http.tasks) == 1 and len(rig.http.notes) == 1
    assert len([c for c in rig.http.api_calls() if c[1].endswith("/upsert")]) == 1


def test_a_request_that_failed_halfway_resumes_without_a_second_upsert():
    rig = Rig()
    fields = {"name": "Jane Doe", "email": JANE}
    rig.http.script(400, {"code": "INVALID_DATA", "message": "invalid data", "status": "error"}, on="Tasks")
    first = rig.capture.capture(fields, "quote_request", "20 desks")
    assert first.status == "failed" and first.vendor_code == "INVALID_DATA" and len(rig.http.leads) == 1 and rig.http.tasks == []
    second = rig.capture.capture(fields, "quote_request", "20 desks")
    assert second.status == "resumed" and len(rig.http.leads) == 1 and len(rig.http.tasks) == 1 and len(rig.http.notes) == 1
    assert len([c for c in rig.http.api_calls() if c[1].endswith("/upsert")]) == 1


def test_the_key_depends_on_tenant_contact_and_day_and_hides_the_email():
    k = idempotency_key("a", "Jane@Example.com", FRIDAY)
    assert k == idempotency_key("a", " jane@example.com ", FRIDAY)
    assert k != idempotency_key("b", "jane@example.com", FRIDAY) and k != idempotency_key("a", "jane@example.com", date(2026, 11, 2))
    assert k != idempotency_key("a", "other@example.com", FRIDAY) and "jane" not in k


# ---- failure never changes the reply -----------------------------------------------------------------------------------------

def test_a_crm_failure_leaves_the_reply_unchanged_and_records_the_vendors_code():
    healthy = Rig()
    _, (_, ok) = happy(healthy)
    broken = Rig()
    broken.http.script(400, {"code": "MANDATORY_NOT_FOUND", "message": "required field not found", "status": "error"})
    _, (_, bad) = happy(broken)
    assert bad.reply == ok.reply and bad.action == ok.action == "replied"
    assert bad.enquiry.lead_status == "failed" and bad.enquiry.vendor_code == "MANDATORY_NOT_FOUND" and not bad.enquiry.captured
    assert any("lead capture failed: MANDATORY_NOT_FOUND" in s and "reply is unchanged" in s for s in bad.steps)
    assert broken.http.leads == {}


def test_an_unexpected_crm_bug_also_leaves_the_reply_alone():
    rig = Rig()
    rig.crm.upsert_lead = lambda *a, **k: (_ for _ in ()).throw(ValueError("boom"))
    _, (_, res) = happy(rig)
    assert res.action == "replied" and "Thanks, Jane." in res.reply and res.enquiry.vendor_code == "UNEXPECTED"


def test_a_bug_in_the_capture_step_itself_cannot_cost_the_visitor_their_reply():
    rig = Rig()
    rig.desk.ready = lambda key: (_ for _ in ()).throw(ValueError("boom"))
    _, (_, res) = happy(rig)
    assert res.action == "replied" and "Thanks, Jane." in res.reply
    assert any("lead capture error: ValueError" in s for s in res.steps)


def test_a_transport_outage_is_retried_by_the_client_and_the_visitor_still_gets_the_same_reply():
    rig = Rig()
    rig.http.drop(1)
    _, (_, res) = happy(rig)
    assert res.enquiry.lead_status == "created" and len(rig.http.leads) == 1


def test_a_failed_capture_is_tried_again_on_the_next_answered_turn_but_never_on_an_escalated_one():
    rig = Rig()
    for _ in range(4):
        rig.http.script(503)  # the client's four attempts all fail
    a = rig.agent([tri("quote_request", FULL), tri("general"), tri("general", angry=True), tri("general")])
    r1, r2 = chat(a, [QUOTE_MSG, "yes"])
    assert r2.enquiry.lead_status == "failed" and rig.http.leads == {}
    calls_before = len(rig.http.api_calls())
    r3 = a.handle("This is useless, I am furious", JANE, "c1")  # the model flags it: escalated, so no retry on this turn
    assert r3.action == "escalated" and len(rig.http.api_calls()) == calls_before
    r4 = a.handle("any news?", JANE, "c1")
    assert r4.action == "replied" and r4.enquiry.lead_status == "created" and len(rig.http.leads) == 1


def test_capture_stops_trying_after_three_failed_attempts():
    rig = Rig()
    for _ in range(40):
        rig.http.script(400, {"code": "INVALID_DATA", "message": "invalid data", "status": "error"})
    a = rig.agent([tri("quote_request", FULL)] + [tri("general")] * 6)
    results = chat(a, [QUOTE_MSG, "yes", "hello", "hello", "hello", "hello"])
    assert [r.enquiry.lead_status for r in results].count("failed") == 3


def test_a_yes_on_a_turn_the_floor_escalates_is_never_recorded():
    rig = Rig()
    a = rig.agent([tri("quote_request", FULL)])
    chat(a, [QUOTE_MSG])
    res = a.handle("Yes, and if you get this wrong I will sue you", JANE, "c1")
    assert res.action == "escalated" and rig.http.calls == []
    assert rig.desk.tracker.get(rig.desk.key("c1", JANE)).consent is None


# ---- audit and privacy -------------------------------------------------------------------------------------------------------

def test_the_audit_log_has_counts_and_kinds_and_none_of_the_visitors_words(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    rig = Rig(audit=log)
    happy(rig)
    raw = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    lines = [json.loads(line) for line in raw.splitlines()]
    last = lines[-1]
    assert last["enquiry"] == {"intent": "quote_request", "queue": "sales", "consent": True, "captured": True, "lead": "created",
                               "vendor": "SUCCESS", "fields_sent": 3}
    for secret in ("jane.doe", "Jane", "Doe", "desks", "quote for", "20 "):
        assert secret not in raw.replace("quote_request", ""), secret
    assert lines[0]["customer"] == "j***@example.com"  # the masked form the repo already uses and lines[0]["enquiry"]["consent"] is False
    assert lines[0]["enquiry"]["intent"] == "quote_request"


def test_a_failed_capture_is_in_the_audit_as_a_vendor_code_not_a_message_with_an_email(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    rig = Rig(audit=log)
    rig.http.script(400, {"code": "INVALID_DATA", "message": "bad value jane.doe@example.com", "status": "error"})
    happy(rig)
    raw = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "INVALID_DATA" in raw and "jane.doe" not in raw and "[email]" in raw


def test_the_australian_domains_are_used_when_the_account_is_australian():
    rig = Rig(region="au")
    happy(rig)
    hosts = {c[1].split("/")[2] for c in rig.http.calls}
    assert hosts == {"accounts.zoho.com.au", "www.zohoapis.com.au"} and len(rig.http.leads) == 1


def test_read_fields_unit_cases():
    out, notes = read_fields({"name": "Jane Doe"}, "I'm Jane Doe, jane@example.com, +63 965 935 1300", "")
    assert out == {"name": "Jane Doe", "email": "jane@example.com", "phone": "+639659351300"} and notes == []
    out, _ = read_fields(None, "my name is Priya Nair", "priya@example.com")
    assert out == {"name": "Priya Nair", "email": "priya@example.com"}
    out, _ = read_fields({"name": "Yes"}, "yes", "x@example.com")
    assert "name" not in out
    out, _ = read_fields({"name": "Jane 7"}, "Jane 7", "")
    assert "name" not in out
    out, _ = read_fields({"name": "DROP TABLE leads; --"}, "DROP TABLE leads; --", "")
    assert "name" not in out
