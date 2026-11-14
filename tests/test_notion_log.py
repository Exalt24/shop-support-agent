"""The Notion ticket log against the in-process FAKE (agent/notion_fake.py). No network, no token, no workspace. Each rule is one the saved
vendor pages state, and each has a mutant in mutation_check.py."""
import json
import random

import pytest

from agent.discord_fake import Signer, click
from agent.discord_handoff import InteractionDesk
from agent.notion_fake import FakeNotion, rate_limited
from agent.notion_log import MAX_RETRY_WAIT, NOTION_VERSION, NotionTicketLog, row_properties

TICKET = {"id": "T-1", "queue": "support", "reason": "legal threat", "intent": "human_needed",
          "email": "jane@example.com", "message": "my phone is 0917 123 4567 and I will sue"}


def log(fake=None, max_attempts=4):
    fake = fake or FakeNotion()
    sleeps = []
    return NotionTicketLog("tok", "DS1", fake, sleep=sleeps.append, max_attempts=max_attempts), fake, sleeps


# ---- the request on the wire ------------------------------------------------------------------------------------------

def test_a_new_ticket_is_looked_up_then_created_with_the_documented_headers_and_body():
    n, fake, _ = log()
    r = n.record(TICKET)
    assert r.status == "created" and r.page_id
    (m1, u1, b1), (m2, u2, b2) = fake.calls
    assert (m1, u1) == ("POST", "https://api.notion.com/v1/data_sources/DS1/query")
    assert b1["filter"] == {"property": "Ticket", "title": {"equals": "T-1"}}
    assert (m2, u2) == ("POST", "https://api.notion.com/v1/pages")
    assert b2["parent"] == {"data_source_id": "DS1"}
    assert fake.status_of("T-1") == "Open"


def test_the_version_header_is_always_sent():
    seen = []

    class Spy(FakeNotion):
        def request(self, method, url, *, headers, params=None, body=None):
            seen.append(headers.get("Notion-Version"))
            return super().request(method, url, headers=headers, params=params, body=body)

    n, _, _ = log(Spy())
    n.record(TICKET)
    assert seen and set(seen) == {NOTION_VERSION}


def test_the_row_holds_no_customer_words_email_or_phone():
    n, fake, _ = log()
    n.record(TICKET)
    blob = json.dumps(fake.calls) + json.dumps(fake.rows)
    for secret in ("jane@example.com", "0917", "sue", "my phone"):
        assert secret not in blob.replace("issue", "")


def test_long_fields_are_cut_inside_notion_limits():
    props = row_properties({**TICKET, "reason": "r" * 5000, "queue": "q" * 500, "intent": "i" * 500, "id": "x" * 500})
    assert len(props["Reason"]["rich_text"][0]["text"]["content"]) <= 300
    assert len(props["Ticket"]["title"][0]["text"]["content"]) <= 100 and len(props["Queue"]["select"]["name"]) <= 80


# ---- exactly one row per ticket ----------------------------------------------------------------------------------------

def test_a_second_record_of_the_same_ticket_makes_no_call():
    n, fake, _ = log()
    n.record(TICKET)
    calls = len(fake.calls)
    assert n.record(TICKET).status == "duplicate" and len(fake.calls) == calls


def test_a_new_process_finds_the_existing_row_and_does_not_create_another():
    n, fake, _ = log()
    n.record(TICKET)
    again, _, _ = log(fake)
    r = again.record(TICKET)
    assert r.status == "duplicate" and fake.count("T-1") == 1


def test_a_lost_answer_to_the_create_is_retried_without_a_second_row():
    n, fake, _ = log()
    fake.lose_response()
    # the first call (the lookup) succeeds, the create is applied and its answer lost, the retry looks the ticket up again
    r = n.record(TICKET)
    assert r.status in ("created", "duplicate") and fake.count("T-1") == 1


def test_two_look_alike_tickets_are_two_rows():
    n, fake, _ = log()
    n.record({**TICKET, "id": "T-1"})
    n.record({**TICKET, "id": "T-10"})
    assert fake.count("T-1") == 1 and fake.count("T-10") == 1


# ---- rate limits and faults --------------------------------------------------------------------------------------------

def test_a_429_waits_the_retry_after_header_then_succeeds():
    n, fake, sleeps = log()
    fake.script(*rate_limited(7))
    assert n.record(TICKET).status == "created" and sleeps == [7.0]


def test_a_529_is_retried_like_a_429():
    n, fake, sleeps = log()
    fake.script(529, {"code": "service_overload"}, {"Retry-After": "2"})
    assert n.record(TICKET).status == "created" and sleeps == [2.0]


def test_a_huge_retry_after_is_capped():
    n, fake, sleeps = log()
    fake.script(*rate_limited(100000))
    n.record(TICKET)
    assert sleeps == [MAX_RETRY_WAIT]


def test_a_429_without_a_readable_header_waits_one_second():
    n, fake, sleeps = log()
    fake.script(429, None, {})
    n.record(TICKET)
    assert sleeps == [1.0]


def test_a_blocked_connection_is_not_retried():
    n, fake, sleeps = log()
    fake.script(*rate_limited(5, reason="public_api_request_blocked"))
    r = n.record(TICKET)
    assert r.status == "refused" and r.code == "blocked" and sleeps == [] and len(fake.calls) == 1


def test_a_rate_limit_that_never_clears_ends_as_refused():
    n, fake, _ = log(max_attempts=3)
    for _ in range(3):
        fake.script(*rate_limited(1))
    r = n.record(TICKET)
    assert r.status == "refused" and r.code == "exhausted" and fake.count("T-1") == 0


def test_a_5xx_is_retried_with_growing_waits():
    n, fake, sleeps = log()
    fake.script(500, None)
    fake.script(502, None)
    assert n.record(TICKET).status == "created" and sleeps == [0.5, 1.0]


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_a_refusal_a_retry_cannot_fix_is_not_retried(status):
    n, fake, sleeps = log()
    fake.script(status, {"code": "x"})
    r = n.record(TICKET)
    assert r.status == "refused" and r.code == f"http_{status}" and len(fake.calls) == 1 and sleeps == []


def test_a_dropped_connection_is_retried():
    n, fake, _ = log()
    fake.drop()
    assert n.record(TICKET).status == "created"


def test_a_wrong_token_is_a_401_and_no_row_exists():
    fake = FakeNotion(token="right")
    n = NotionTicketLog("wrong", "DS1", fake, sleep=lambda s: None)
    assert n.record(TICKET).code == "http_401" and not fake.rows


# ---- status changes ----------------------------------------------------------------------------------------------------

def test_set_status_patches_the_row():
    n, fake, _ = log()
    n.record(TICKET)
    assert n.set_status("T-1", "taken").status == "updated" and fake.status_of("T-1") == "Taken"
    assert n.set_status("T-1", "resolved").status == "updated" and fake.status_of("T-1") == "Resolved"


def test_set_status_finds_a_row_made_by_another_process():
    n, fake, _ = log()
    n.record(TICKET)
    other, _, _ = log(fake)
    assert other.set_status("T-1", "taken").status == "updated" and fake.status_of("T-1") == "Taken"


def test_set_status_for_an_unknown_ticket_or_state_is_refused_and_creates_nothing():
    n, fake, _ = log()
    assert n.set_status("T-9", "taken").status == "refused" and not fake.rows
    n.record(TICKET)
    assert n.set_status("T-1", "burned").code == "bad_state" and fake.status_of("T-1") == "Open"


def test_a_discord_click_moves_the_notion_status_and_a_failing_log_does_not_break_the_click():
    n, fake, _ = log()
    n.record(TICKET)
    signer, audit = Signer(), []
    desk = InteractionDesk(signer.public_hex, audit.append, on_change=lambda tid, st, who: n.set_status(tid, st))
    desk.register(TICKET)
    h, raw = signer.sign(click("take:T-1", "u7"))
    status, out = desk.handle(h, raw)
    assert status == 200 and out["type"] == 7 and fake.status_of("T-1") == "Taken"

    def boom(tid, st, who):
        raise RuntimeError("notion down")
    desk2 = InteractionDesk(signer.public_hex, audit.append, on_change=boom)
    desk2.register(TICKET)
    h, raw = signer.sign(click("take:T-1", "u7"))
    status, out = desk2.handle(h, raw)
    assert status == 200 and out["type"] == 7 and {"discord": "on_change_failed", "ticket": "T-1"} in audit


# ---- seeded random worlds -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("seed", range(40))
def test_random_worlds_keep_the_invariants(seed):
    rng = random.Random(seed)
    fake = FakeNotion()
    sleeps = []
    logs = [NotionTicketLog("tok", "DS1", fake, sleep=sleeps.append, max_attempts=4) for _ in range(rng.randint(1, 3))]
    ids = [f"T-{rng.choice(['1', '10', '100'])}{i}" for i in range(rng.randint(2, 6))]
    order = {"Open": 0, "Taken": 1, "Resolved": 2}
    floor: dict[str, int] = {}
    for _ in range(rng.randint(15, 60)):
        n, tid = rng.choice(logs), rng.choice(ids)
        fault = rng.choice([None, None, None, "429", "529", "500", "drop", "lose", "403"])
        if fault == "429":
            fake.script(*rate_limited(rng.randint(1, 500)))
        elif fault == "529":
            fake.script(529, None, {"Retry-After": "3"})
        elif fault == "500":
            fake.script(500, None)
        elif fault == "403":
            fake.script(403, {"code": "restricted_resource"})
        elif fault == "drop":
            fake.drop()
        elif fault == "lose":
            fake.lose_response()
        if rng.random() < 0.55:
            n.record({"id": tid, "queue": rng.choice(["support", "sales"]), "reason": "legal threat", "message": "secret words " + tid, "email": tid + "@example.com"})
        else:
            state = rng.choice(["taken", "resolved"])
            n.set_status(tid, state)
        st = fake.status_of(tid)
        if st:
            assert order[st] >= 0
    assert all(fake.count(t) <= 1 for t in ids), "one row per ticket, even through lost answers and several processes"
    assert all(s <= MAX_RETRY_WAIT for s in sleeps)
    blob = json.dumps(fake.calls)
    assert "@example.com" not in blob and "secret words" not in blob
