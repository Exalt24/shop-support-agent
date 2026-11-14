"""Google Calendar booking against the in-process FAKE (agent/gcal_fake.py). No network, no Google account. Each rule is one the
saved vendor pages state (docs/vendor-contract/gcal-*.txt), and each has a mutant in mutation_check.py."""
import json
import random
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from agent.core import SupportAgent
from agent.gcal_booking import (API_BASE, BookingChat, BookingDesk, GoogleCalendar, GoogleCalendarError, MAX_BACKOFF, Rules, Slot,
                                clean_name, event_id_for)
from agent.gcal_fake import FakeGoogleCalendar, error_body
from agent.zoho_crm import TransportError
from conftest import FakeShop, NoLLM, ScriptedLLM

CHI = ZoneInfo("America/Chicago")
NOW = datetime(2026, 11, 16, 14, 0, tzinfo=timezone.utc)          # Monday 8:00 AM in Chicago (CST)
EMAIL = "jane@example.com"


def local(day, hour, minute=0, month=11):
    return datetime(2026, month, day, hour, minute, tzinfo=CHI)


def make(fake=None, rules=None, now=NOW, calendar_id=None, **cal_kw):
    fake = fake or FakeGoogleCalendar()
    sleeps = []
    cal = GoogleCalendar(calendar_id or fake.calendar, fake.provider(), fake, sleep=sleeps.append, **cal_kw)
    return BookingDesk(cal, rules, now=lambda: now), fake, sleeps


def busy(fake, start, end):
    fake.external_busy.append((start, end))


def slot(day, hour, minute=0):
    s = local(day, hour, minute)
    return Slot(s, s + timedelta(minutes=30))


# ---- the wire ------------------------------------------------------------------------------------------------------------

def test_freebusy_sends_the_documented_url_and_body():
    d, fake, _ = make()
    d.offers()
    method, url, _, body = fake.calls[0]
    assert (method, url) == ("POST", f"{API_BASE}/freeBusy")
    assert body["items"] == [{"id": fake.calendar}] and body["timeZone"] == "America/Chicago"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", body["timeMin"]) and body["timeMin"] < body["timeMax"]


def test_every_call_carries_the_bearer_token():
    d, fake, _ = make()
    assert d.offers()                                # the fake refuses any call whose Authorization is not "Bearer <valid token>"
    other = FakeGoogleCalendar()
    cal = GoogleCalendar(other.calendar, lambda force=False: "not-the-token", other, sleep=lambda s: None)
    with pytest.raises(GoogleCalendarError) as e:
        cal.busy(NOW, NOW + timedelta(days=1), "UTC")
    assert e.value.status == 401


def test_a_401_gets_one_new_token_and_the_call_is_repeated():
    fake = FakeGoogleCalendar()
    cal = GoogleCalendar(fake.calendar, fake.provider(first="stale"), fake, sleep=lambda s: None)
    assert cal.busy(NOW, NOW + timedelta(days=1), "UTC") == []
    assert fake.refreshes == 1 and len(fake.calls) == 2


def test_a_second_401_is_final_and_the_token_is_refreshed_only_once():
    fake, forced = FakeGoogleCalendar(), []

    def provider(force=False):
        forced.append(force)
        return "always-bad"
    cal = GoogleCalendar(fake.calendar, provider, fake, sleep=lambda s: None)
    with pytest.raises(GoogleCalendarError) as e:
        cal.busy(NOW, NOW + timedelta(days=1), "UTC")
    assert e.value.status == 401 and forced.count(True) == 1


@pytest.mark.parametrize("status,reason", [(403, "rateLimitExceeded"), (403, "userRateLimitExceeded"), (429, "rateLimitExceeded"),
                                           (500, "backendError"), (503, "backendError")])
def test_limits_and_outages_back_off_and_retry(status, reason):
    d, fake, sleeps = make()
    fake.script(status, error_body(status, "usageLimits", reason, "x"))
    fake.script(status, error_body(status, "usageLimits", reason, "x"))
    assert d.offers()
    assert len(sleeps) == 2 and sleeps[1] > sleeps[0]


@pytest.mark.parametrize("status,reason", [(403, "quotaExceeded"), (403, "forbiddenForNonOrganizer"), (400, "timeRangeEmpty"), (404, "notFound")])
def test_permanent_errors_are_not_retried(status, reason):
    d, fake, sleeps = make()
    fake.script(status, error_body(status, "calendar", reason, "x"))
    with pytest.raises(GoogleCalendarError) as e:
        d.offers()
    assert e.value.status == status and len(fake.calls) == 1 and sleeps == []


def test_retries_end_after_the_attempt_limit():
    d, fake, sleeps = make(max_attempts=3)
    for _ in range(3):
        fake.script(500, error_body(500, "global", "backendError", "x"))
    with pytest.raises(GoogleCalendarError) as e:
        d.offers()
    assert e.value.status == 500 and len(fake.calls) == 3 and len(sleeps) == 2


def test_backoff_doubles_and_is_capped():
    d, fake, sleeps = make(max_attempts=12)
    for _ in range(11):
        fake.script(429, error_body(429, "usageLimits", "rateLimitExceeded", "x"))
    assert d.offers()
    assert sleeps == sorted(sleeps) and sleeps[0] == 0.5 and sleeps[1] == 1.0 and max(sleeps) == MAX_BACKOFF


def test_a_dropped_connection_is_retried():
    d, fake, sleeps = make()
    fake.drop(2)
    assert d.offers() and len(sleeps) == 2


def test_a_dropped_connection_that_never_recovers_raises_a_calendar_error():
    d, fake, _ = make(max_attempts=2)
    fake.drop(5)
    with pytest.raises(GoogleCalendarError) as e:
        d.offers()
    assert e.value.reason == "transport"


# ---- reading the calendar: errors are never "free" -----------------------------------------------------------------------

def test_a_calendar_that_comes_back_with_errors_is_not_read_as_free():
    d, fake, _ = make(calendar_id="someone-else@example.com")          # the fake answers 200 with errors[] notFound
    with pytest.raises(GoogleCalendarError) as e:
        d.offers()
    assert e.value.reason == "calendar_not_read"


def test_a_free_busy_answer_with_no_entry_for_the_calendar_is_not_free():
    d, fake, _ = make()
    fake.script(200, {"kind": "calendar#freeBusy", "calendars": {}})
    with pytest.raises(GoogleCalendarError):
        d.offers()


def test_an_unreadable_busy_time_is_not_free():
    d, fake, _ = make()
    fake.script(200, {"calendars": {fake.calendar: {"busy": [{"start": "soon", "end": "later"}]}}})
    with pytest.raises(GoogleCalendarError) as e:
        d.offers()
    assert e.value.reason == "unreadable_busy_time"


# ---- which times are offered ---------------------------------------------------------------------------------------------

def test_the_first_offer_is_two_hours_ahead_and_the_offers_are_three_different_days():
    d, _, _ = make()
    got = d.offers()
    assert [s.start for s in got] == [local(16, 10), local(17, 9), local(18, 9)]


def test_a_weekend_is_skipped():
    d, _, _ = make(now=datetime(2026, 11, 19, 14, 0, tzinfo=timezone.utc))          # Thursday 8:00
    assert [s.start for s in d.offers()] == [local(19, 10), local(20, 9), local(23, 9)]


def test_a_busy_block_moves_the_offer_to_the_next_free_slot():
    d, fake, _ = make()
    busy(fake, local(16, 10), local(16, 10, 30))
    assert d.offers()[0].start == local(16, 10, 30)


def test_a_slot_that_starts_when_a_busy_block_ends_is_free():
    d, fake, _ = make()
    busy(fake, local(17, 9), local(17, 10))
    assert d.offers()[1].start == local(17, 10)


def test_a_slot_that_ends_when_a_busy_block_starts_is_free():
    d, fake, _ = make()
    busy(fake, local(17, 9, 30), local(17, 12))
    assert d.offers()[1].start == local(17, 9)


def test_a_slot_that_would_run_past_closing_is_never_offered():
    d, fake, _ = make(rules=Rules(slot_minutes=45))
    busy(fake, local(17, 9), local(17, 16, 30))             # only 16:30 would be left, and 16:30 + 45 minutes is after 17:00
    assert [s.start.day for s in d.offers()] == [16, 18, 19]


def test_nothing_is_offered_beyond_the_horizon():
    d, fake, _ = make(rules=Rules(horizon_days=3))
    busy(fake, local(16, 0), local(20, 0))                 # the whole horizon is taken; Friday the 20th is free but too far
    assert d.offers() == []


def test_the_number_of_offers_is_the_rule():
    d, _, _ = make(rules=Rules(offer=2))
    assert len(d.offers()) == 2


def test_offers_keep_the_business_wall_clock_across_the_end_of_summer_time():
    d, _, _ = make(now=datetime(2026, 10, 30, 14, 0, tzinfo=timezone.utc))          # Friday, still CDT
    got = d.offers()
    assert [s.start.hour for s in got] == [11, 9, 9] and got[0].start.utcoffset() == timedelta(hours=-5)
    assert got[1].start.day == 2 and got[1].start.utcoffset() == timedelta(hours=-6)


# ---- booking -------------------------------------------------------------------------------------------------------------

def test_a_booking_creates_one_event_with_the_documented_shape():
    d, fake, _ = make()
    r = d.book(slot(17, 9), "Maria Santos", EMAIL)
    assert r.status == "booked" and len(fake.events) == 1
    ev = next(iter(fake.events.values()))
    assert ev["summary"] == "Appointment: Maria Santos" and ev["attendees"] == [{"email": EMAIL}]
    assert ev["start"]["timeZone"] == "America/Chicago" and ev["start"]["dateTime"] == local(17, 9).isoformat()
    assert ev["_sendUpdates"] == "all" and re.fullmatch(r"[a-v0-9]{5,1024}", ev["id"])
    assert fake.calls[-1][2] == {"sendUpdates": "all"}


def test_the_event_id_is_stable_per_slot_and_visitor_and_ignores_email_case():
    a = event_id_for("c", local(17, 9), "Jane@Example.com")
    assert a == event_id_for("c", local(17, 9), " jane@example.com ")
    assert a != event_id_for("c", local(17, 9, 30), "jane@example.com") and a != event_id_for("c", local(17, 9), "joe@example.com")
    assert re.fullmatch(r"[a-v0-9]{5,1024}", a)


def test_booking_the_same_slot_twice_makes_one_event():
    d, fake, _ = make()
    first = d.book(slot(17, 9), "Maria Santos", EMAIL)
    second = d.book(slot(17, 9), "Maria Santos", EMAIL)
    # the slot is now busy because of the first event, so the second is "taken"; either way no second event exists
    assert first.status == "booked" and second.status == "taken" and len(fake.events) == 1


def test_a_slot_that_became_busy_after_it_was_offered_is_refused_without_writing():
    d, fake, _ = make()
    chosen = d.offers()[0]
    busy(fake, chosen.start, chosen.end)
    r = d.book(chosen, "Maria Santos", EMAIL)
    assert r.status == "taken" and fake.events == {} and not any(c[0] == "POST" and c[1].endswith("/events") for c in fake.calls)


@pytest.mark.parametrize("bad", [slot(21, 10), slot(17, 8), slot(17, 17), slot(16, 9), slot(17, 9, 15)])          # Sat, early, late, inside the lead time, off the grid
def test_a_time_that_is_not_one_of_the_business_slots_is_refused(bad):
    d, fake, _ = make()
    assert d.book(bad, "Maria Santos", EMAIL).status == "unavailable" and fake.events == {}


def test_a_slot_of_the_wrong_length_is_refused():
    d, fake, _ = make()
    s = local(17, 9)
    assert d.book(Slot(s, s + timedelta(hours=3)), "Maria Santos", EMAIL).status == "unavailable"


@pytest.mark.parametrize("bad", ["0917 123 4567", "jane@example.com", "<script>", "Maria 2", "", "A", "one two three four five"])
def test_something_that_is_not_a_name_is_never_written_to_the_calendar(bad):
    d, fake, _ = make()
    assert clean_name(bad) is None
    assert d.book(slot(17, 9), bad, EMAIL).status == "refused" and fake.events == {}


@pytest.mark.parametrize("good", ["Maria Santos", "Jean-Luc O'Neil", "Dr. Ana Cruz", "Li"])
def test_ordinary_names_are_accepted(good):
    assert clean_name(good) == good


def test_without_a_valid_email_there_is_no_guest_and_no_invitation():
    d, fake, _ = make()
    assert d.book(slot(17, 9), "Maria Santos", "not-an-email").status == "booked"
    ev = next(iter(fake.events.values()))
    assert "attendees" not in ev and ev["_sendUpdates"] == "none"


def test_send_updates_must_be_a_documented_value():
    d, fake, _ = make()
    with pytest.raises(ValueError):
        d.calendar.insert("abcde12345", {"start": {"dateTime": "x"}, "end": {"dateTime": "y"}}, "yes")


def test_a_failed_free_busy_read_before_the_write_refuses_the_booking():
    d, fake, _ = make(max_attempts=1)
    fake.script(403, error_body(403, "usageLimits", "quotaExceeded", "x"))
    assert d.book(slot(17, 9), "Maria Santos", EMAIL).status == "refused" and fake.events == {}


# ---- the id is not a guarantee: look before sending again -----------------------------------------------------------------

def test_a_lost_answer_is_looked_up_before_the_event_is_sent_again():
    fake = FakeGoogleCalendar(detect_collisions=False)          # the saved page says collisions may not be detected
    d, fake, _ = make(fake)
    fake.lose_response()
    r = d.book(slot(17, 9), "Maria Santos", EMAIL)
    assert r.status == "duplicate" and len(fake.events) == 1 and fake.duplicate_inserts == 0


def test_a_lost_answer_with_collision_detection_still_ends_with_one_event():
    d, fake, _ = make()
    fake.lose_response()
    assert d.book(slot(17, 9), "Maria Santos", EMAIL).status == "duplicate" and len(fake.events) == 1


def test_a_server_error_before_anything_was_created_is_resent_once_the_lookup_found_nothing():
    d, fake, _ = make()
    fake.script(500, error_body(500, "global", "backendError", "x"), match="/events")
    r = d.book(slot(17, 9), "Maria Santos", EMAIL)
    assert r.status == "booked" and len(fake.events) == 1


def test_a_server_error_after_the_event_was_created_is_looked_up_before_a_resend():
    d, fake, _ = make(FakeGoogleCalendar(detect_collisions=False))
    fake.lose_response(status=500)
    r = d.book(slot(17, 9), "Maria Santos", EMAIL)
    assert r.status == "duplicate" and len(fake.events) == 1 and fake.duplicate_inserts == 0


def test_getting_an_event_that_does_not_exist_is_none():
    d, fake, _ = make()
    assert d.calendar.get("abcde12345") is None


def test_a_failed_lookup_does_not_stop_the_booking():
    d, fake, _ = make()
    fake.lose_response()
    fake.script(403, error_body(403, "usageLimits", "quotaExceeded", "x"), match="GET")
    r = d.book(slot(17, 9), "Maria Santos", EMAIL)
    assert r.status == "duplicate" and len(fake.events) == 1


def test_a_409_for_the_same_start_is_a_success_and_for_another_start_is_a_collision():
    d, fake, _ = make()
    s = slot(17, 9)
    eid = event_id_for(fake.calendar, s.start, EMAIL)
    fake.events[eid] = {"id": eid, "start": {"dateTime": s.start.isoformat()}, "end": {"dateTime": s.end.isoformat()}}
    # the slot is held by that event, so free/busy says taken; ask the insert directly to see the 409 handling
    same = d.calendar.insert(eid, {"start": {"dateTime": s.start.isoformat()}, "end": {"dateTime": s.end.isoformat()}})
    assert same.status == "duplicate"
    other = slot(17, 10)
    with pytest.raises(GoogleCalendarError) as e:
        d.calendar.insert(eid, {"start": {"dateTime": other.start.isoformat()}, "end": {"dateTime": other.end.isoformat()}})
    assert e.value.reason == "id_collision"


def test_the_insert_posts_the_event_to_the_calendars_events_url():
    d, fake, _ = make()
    d.book(slot(17, 9), "Maria Santos", EMAIL)
    method, url, _, body = fake.calls[-1]
    assert (method, url) == ("POST", f"{API_BASE}/calendars/{fake.calendar}/events") and body["id"]


# ---- the conversation ----------------------------------------------------------------------------------------------------

def chat(**kw):
    d, fake, sleeps = make(**kw)
    return BookingChat(d), d, fake


def say(c, message, key="k1", email=EMAIL):
    steps = []
    return c.converse(key, message, email, steps), steps


def test_a_visitor_books_in_three_messages():
    c, d, fake = chat()
    t1, _ = say(c, "Hi, I would like to book an appointment")
    assert t1.reply.startswith("These times are free: 1) Mon 16 Nov at 10:00 AM CST, 2) Tue 17 Nov at 9:00 AM CST, 3) Wed 18 Nov at 9:00 AM CST")
    t2, _ = say(c, "2")
    assert t2.reply == "What name should I put the booking under?"
    t3, _ = say(c, "Maria Santos")
    assert t3.reply == "You are booked for Tue 17 Nov at 9:00 AM CST under the name Maria Santos. An invitation is on its way to your email."
    assert t3.escalate is None and len(fake.events) == 1 and "k1" not in c.states


def test_ordinal_words_pick_a_time():
    c, _, _ = chat()
    say(c, "can I book an appointment")
    t, _ = say(c, "the third one please")
    assert t.reply == "What name should I put the booking under?" and c.states["k1"].chosen.start == local(18, 9)


def test_an_unclear_choice_is_asked_again_once_and_then_goes_to_a_person():
    c, _, fake = chat()
    say(c, "book an appointment")
    first, _ = say(c, "1 or 2")
    assert first.escalate is None and "1, 2 or 3" in first.reply
    second, steps = say(c, "whichever")
    assert second.escalate == "booking choice unclear" and "k1" not in c.states and fake.events == {}


def test_a_number_that_was_not_offered_is_not_a_choice():
    c, _, _ = chat()
    say(c, "book an appointment")
    t, _ = say(c, "5")
    assert "1, 2 or 3" in t.reply and c.states["k1"].step == "offered"


def test_cancel_clears_the_conversation_and_books_nothing():
    c, _, fake = chat()
    say(c, "book an appointment")
    say(c, "2")
    t, _ = say(c, "never mind")
    assert "not booked anything" in t.reply and "k1" not in c.states and fake.events == {}


@pytest.mark.parametrize("text", ["where is my order #1019", "add it to my address book", "what is your returns policy", "hello"])
def test_a_message_that_is_not_about_booking_is_left_to_the_agent(text):
    c, _, fake = chat()
    t, _ = say(c, text)
    assert t is None and fake.calls == []


def test_an_old_conversation_is_dropped():
    c, d, _ = chat()
    say(c, "book an appointment")
    later = NOW + timedelta(minutes=31)
    d._now = lambda: later
    t, steps = say(c, "2")
    assert t is None and "k1" not in c.states and any("old conversation" in s for s in steps)


def test_a_time_taken_between_the_offer_and_the_name_makes_a_new_offer():
    c, d, fake = chat()
    say(c, "book an appointment")
    say(c, "1")
    chosen = c.states["k1"].chosen
    busy(fake, chosen.start, chosen.end)
    t, _ = say(c, "Maria Santos")
    assert t.reply.startswith("That time is no longer free. These times are free:") and c.states["k1"].step == "offered"
    assert "10:00 AM" not in t.reply.split("free:")[1].split(",")[0] or "Mon 16 Nov" not in t.reply.split("free:")[1].split(",")[0]
    assert fake.events == {}


def test_an_unreadable_calendar_goes_to_a_person_and_says_so():
    c, _, _ = chat(calendar_id="someone-else@example.com")
    t, steps = say(c, "book an appointment")
    assert t.escalate == "calendar unavailable" and "k1" not in c.states and any("calendar not read" in s for s in steps)


def test_no_free_time_goes_to_a_person():
    c, d, fake = chat(rules=Rules(horizon_days=1))
    busy(fake, local(16, 0), local(19, 0))
    t, _ = say(c, "book an appointment")
    assert t.escalate == "no free booking time"


def test_a_name_that_is_not_a_name_is_asked_again_once_and_then_goes_to_a_person():
    c, _, fake = chat()
    say(c, "book an appointment")
    say(c, "1")
    first, _ = say(c, "my number is 0917 123 4567")
    assert first.escalate is None and "just your name" in first.reply
    second, _ = say(c, "jane@example.com")
    assert second.escalate == "booking name unclear" and fake.events == {}


def test_a_refused_booking_goes_to_a_person():
    c, d, fake = chat()
    say(c, "book an appointment")
    say(c, "1")
    fake.script(400, error_body(400, "global", "invalid", "x"), match="/events")
    t, steps = say(c, "Maria Santos")
    assert t.escalate == "booking failed" and "k1" not in c.states and fake.events == {}


def test_what_the_conversation_audits_holds_no_name_and_no_email():
    c, _, _ = chat()
    all_steps = []
    for m in ("book an appointment", "2", "Maria Santos"):
        _, steps = say(c, m)
        all_steps += steps
    text = " ".join(all_steps)
    assert "Maria" not in text and "jane" not in text and "example.com" not in text and "booking: booked" in text


def test_a_number_after_the_booking_does_not_book_again():
    c, _, fake = chat()
    for m in ("book an appointment", "2", "Maria Santos"):
        say(c, m)
    t, _ = say(c, "2")
    assert t is None and len(fake.events) == 1


def test_two_visitors_do_not_share_a_conversation():
    c, _, fake = chat()
    say(c, "book an appointment", key="a")
    say(c, "book an appointment", key="b")
    say(c, "1", key="a")
    assert c.states["a"].step == "need_name" and c.states["b"].step == "offered"


# ---- inside the agent ----------------------------------------------------------------------------------------------------

def agent(policy, **kw):
    d, fake, _ = make(**kw)
    return SupportAgent(NoLLM(), FakeShop(), policy, booking=BookingChat(d)), fake


def test_the_agent_runs_the_booking_conversation_without_the_model(policy):
    a, fake = agent(policy)
    res = a.handle("I need an appointment please", EMAIL, "conv-1")
    assert res.action == "replied" and res.intent == "booking" and res.reply.startswith("These times are free:")
    a.handle("1", EMAIL, "conv-1")
    done = a.handle("Maria Santos", EMAIL, "conv-1")
    assert done.reply.startswith("You are booked for") and len(fake.events) == 1


def test_a_legal_threat_is_escalated_before_the_calendar_is_touched(policy):
    a, fake = agent(policy)
    res = a.handle("I will sue you, book me an appointment with a lawyer", EMAIL, "c")
    assert res.action == "escalated" and fake.calls == []


def test_a_booking_that_needs_a_person_escalates_the_turn(policy):
    a, fake = agent(policy, calendar_id="someone-else@example.com")
    res = a.handle("I need an appointment please", EMAIL, "c")
    assert res.action == "escalated" and res.escalation_reason == "calendar unavailable" and res.intent == "booking"


def test_without_a_booking_desk_the_agent_is_unchanged(policy):
    llm = ScriptedLLM({"intent": "other", "order_number": None, "product_query": None, "angry": False, "legal_threat": False,
                       "chargeback": False, "wants_human": False})
    res = SupportAgent(llm, FakeShop(), policy).handle("I need an appointment please", EMAIL)
    assert res.action == "escalated" and res.escalation_reason == "out of scope"


def test_the_conversation_key_is_the_conversation_id_or_the_email(policy):
    a, fake = agent(policy)
    a.handle("book an appointment", "A@Example.com")
    assert "a@example.com" in a._booking.states
    a.handle("book an appointment", EMAIL, "Conv-9")
    assert "conv-9" in a._booking.states


# ---- seeded random worlds ------------------------------------------------------------------------------------------------

LABEL = re.compile(r"[A-Z][a-z]{2} \d{2} [A-Z][a-z]{2} at \d{1,2}:\d{2} [AP]M [A-Z]{3}")


class Chaos:
    """Wraps the fake so that any call may be dropped, rate limited, fail with a 500 or lose its answer after the work was done."""

    def __init__(self, fake, rng, p):
        self.fake, self.rng, self.p = fake, rng, p

    def request(self, method, url, *, headers, params=None, body=None):
        roll = self.rng.random()
        if roll < self.p:
            kind = self.rng.choice(["drop", "429", "500", "403", "lose"])
            if kind == "drop":
                self.fake.drop()
            elif kind == "429":
                self.fake.script(429, error_body(429, "usageLimits", "rateLimitExceeded", "x"))
            elif kind == "500":
                self.fake.script(500, error_body(500, "global", "backendError", "x"))
            elif kind == "403":
                self.fake.script(403, error_body(403, "usageLimits", "userRateLimitExceeded", "x"))
            else:
                self.fake.lose_response()
        return self.fake.request(method, url, headers=headers, params=params, body=body)


@pytest.mark.parametrize("seed", range(40))
def test_random_worlds_never_double_book_overbook_or_say_an_unoffered_time(seed):
    rng = random.Random(seed)
    fake = FakeGoogleCalendar(detect_collisions=rng.random() < 0.5)
    for _ in range(rng.randint(0, 6)):                                      # outside meetings, before anyone books
        day, hour = rng.randint(16, 27), rng.randint(9, 16)
        busy(fake, local(day, hour), local(day, hour) + timedelta(minutes=rng.choice([30, 60, 90])))
    cal = GoogleCalendar(fake.calendar, fake.provider(first=rng.choice([None, "stale"])), Chaos(fake, rng, 0.25), sleep=lambda s: None,
                         max_attempts=rng.choice([3, 4, 5]))
    desk = BookingDesk(cal, Rules(slot_minutes=rng.choice([30, 45, 60])), now=lambda: NOW)
    labels: set[str] = set()
    real_offers = desk.offers

    def spying():
        out = real_offers()
        labels.update(s.label() for s in out)
        return out
    desk.offers = spying
    c = BookingChat(desk)
    visitors = [("v%d" % i, "visitor%d@example.com" % i, "Visitor %s" % "ABC"[i]) for i in range(3)]
    for _ in range(rng.randint(8, 24)):
        key, email, name = rng.choice(visitors)
        message = rng.choice(["book an appointment", "1", "2", "3", name, name, "cancel", "hmm", "first", "what is the time"])
        turn = c.converse(key, message, email, [])
        if turn:
            for found in LABEL.findall(turn.reply):
                assert found in labels, "said a time that was never offered: " + found
    assert fake.duplicate_inserts == 0 and fake.overlap_inserts == 0
    seen = Counter((e["start"]["dateTime"], tuple(a["email"] for a in e.get("attendees", []))) for e in fake.events.values())
    assert all(n == 1 for n in seen.values()), "the same visitor booked the same time twice"
    rules = desk.rules
    for e in fake.events.values():
        start = datetime.fromisoformat(e["start"]["dateTime"]).astimezone(CHI)
        end = datetime.fromisoformat(e["end"]["dateTime"]).astimezone(CHI)
        assert start.weekday() in rules.weekdays and start.hour >= rules.open_hour and end <= start.replace(hour=rules.close_hour, minute=0)
        assert end - start == timedelta(minutes=rules.slot_minutes)
