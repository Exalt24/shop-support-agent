"""Lead capture over many seeded random worlds: identities that are the same person written differently, a phone-only contact,
three days, repeated and different enquiries, and faults (a rejected step, a run of 5xx, a dropped connection, an expired
token, a 429) landing between the steps. The rules are invariants, not examples:

  R1  capture never raises, and a failure carries a vendor code and never an email
  R2  one lead per email, whatever the case, however many enquiries, retries and faults
  R3  at most one task per person per day, at most one note per person per day per distinct enquiry
  R4  an undelivered step comes back: the same request retried once the faults stop completes, and every enquiry ends fully
      in the CRM (a retry is the next turn of the same chat, the same day: the clock never runs backwards)
  R5  an identical request after success changes nothing
  R6  a fault the client can absorb (expired token, one 429) is invisible
"""
import random
from datetime import date, timedelta

import pytest

from agent.enquiry import LEAD_INTENTS, ROUTES
from agent.leads import LeadCapture
from agent.zoho_crm import ZohoCrmClient, ZohoSettings
from agent.zoho_fake import FakeZoho

START = date(2026, 10, 30)  # a Friday, so the days cross a weekend
PEOPLE = {  # identity -> fields; the first two are one person written two ways
    "a": {"name": "Ann Lee", "email": "ann@x.com"},
    "a2": {"name": "Ann Lee", "email": "ANN@X.com"},
    "b": {"name": "Bo Chen", "email": "bo@x.com"},
    "c": {"name": "Cy Park", "phone": "09171234567"},
}
SUMMARIES = ["20 desks", "service for the compressor", "a quote for chairs"]
INTENTS_ = sorted(LEAD_INTENTS)


def world(seed, steps=30):
    rng = random.Random(seed)
    http = FakeZoho()
    crm = ZohoCrmClient(ZohoSettings("c", "s", "r"), http, sleep=lambda s: None)
    day = {"d": START}
    cap = LeadCapture(crm, tenant="t", today=lambda: day["d"])
    attempted, statuses, task_tags, note_tags = [], {}, [], []
    for _ in range(steps):
        if rng.random() < 0.2:
            day["d"] += timedelta(days=rng.choice([1, 1, 3]))
        who, intent, summary = rng.choice(list(PEOPLE)), rng.choice(INTENTS_), rng.choice(SUMMARIES)
        fault = rng.choice(["none", "none", "reject_task", "reject_note", "reject_upsert", "outage", "drop", "expire", "429"])
        if fault == "reject_task":
            http.script(400, {"code": "INVALID_DATA", "message": "invalid data", "status": "error"}, on="Tasks")
        elif fault == "reject_note":
            http.script(400, {"code": "INVALID_DATA", "message": "bad jane@x.com", "status": "error"}, on="Notes")
        elif fault == "reject_upsert":
            http.script(400, {"code": "MANDATORY_NOT_FOUND", "message": "required", "status": "error"}, on="upsert")
        elif fault == "outage":
            for _ in range(4):
                http.script(503)
        elif fault == "drop":
            http.drop(4)
        elif fault == "expire":
            http.expire_token()
        elif fault == "429":
            http.script(429)
        n_tasks, n_notes = len(http.tasks), len(http.notes)
        out = cap.capture(dict(PEOPLE[who]), intent, summary)  # R1: must not raise
        assert out.vendor_code and "@" not in out.detail, (seed, out)
        if fault in ("none", "expire", "429"):
            assert out.status != "failed", (seed, fault, out)  # R6
        statuses[out.status] = statuses.get(out.status, 0) + 1
        http._scripted.clear()
        http._drop = 0
        if out.status == "failed":  # R4
            out = cap.capture(dict(PEOPLE[who]), intent, summary)
            assert out.status != "failed", (seed, who, out)
            statuses[out.status] = statuses.get(out.status, 0) + 1
        attempted.append((who, day["d"], intent, summary))
        task_tags += [(t["What_Id"]["id"], day["d"]) for t in http.tasks[n_tasks:]]
        note_tags += [(n["lead_id"], day["d"], n["content"]) for n in http.notes[n_notes:]]
        emails = [(l.get("Email") or "").lower() for l in http.leads.values() if l.get("Email")]
        assert len(emails) == len(set(emails)), (seed, emails)  # R2
        if out.status not in ("failed", "duplicate"):  # R5
            counts = (len(http.leads), len(http.tasks), len(http.notes))
            again = cap.capture(dict(PEOPLE[who]), intent, summary)
            assert again.status == "duplicate" and (len(http.leads), len(http.tasks), len(http.notes)) == counts, (seed, again)
    return http, cap, day, attempted, statuses, task_tags, note_tags


@pytest.mark.parametrize("seed", range(120))
def test_lead_capture_invariants_hold_in_a_random_world(seed):
    http, cap, day, attempted, _, task_tags, note_tags = world(seed)
    # R3, before any recovery pass
    assert len(task_tags) == len(set(task_tags)), (seed, task_tags)
    assert len(note_tags) == len(set(note_tags)), (seed, note_tags)
    by_email = {l["Email"].lower(): i for i, l in http.leads.items() if l.get("Email")}
    for who, d, intent, summary in attempted:
        person = PEOPLE[who]
        if "email" not in person:
            continue  # a phone-only contact has no Email for Zoho to check, so the CRM may hold one lead per day for it (a known limit)
        lead_id = by_email[person["email"].lower()]
        assert any(n["lead_id"] == lead_id and summary in n["content"] and intent.replace("_", " ") in n["content"] for n in http.notes), (seed, who, summary)
        if ROUTES[intent].task_subject:
            assert any(t["What_Id"]["id"] == lead_id for t in http.tasks), (seed, who, intent)
    phone_days = {d for who, d, _, _ in attempted if "email" not in PEOPLE[who]}
    assert len([l for l in http.leads.values() if l.get("Phone")]) <= len(phone_days)  # at most one lead per day, never two on a day


def test_the_generator_really_does_cross_the_dimensions():
    """The property test is only worth its name if the worlds reach the corners. Count what the same seeds produce."""
    total = {}
    for seed in range(120):
        for k, v in world(seed)[4].items():
            total[k] = total.get(k, 0) + v
    assert all(total.get(k, 0) > 20 for k in ("failed", "resumed", "noted", "updated", "duplicate", "created")), total
