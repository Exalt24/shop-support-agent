"""The Discord handoff against the in-process FAKE (agent/discord_fake.py). No network, no bot, no server. Each rule is one the saved
vendor pages state, and each has a mutant in mutation_check.py."""
import json
import random

import pytest

from agent.discord_fake import FakeDiscord, Signer, click
from agent.discord_handoff import (API_BASE, DiscordHandoff, InteractionDesk, MAX_RETRY_WAIT, nonce_for, ticket_message,
                                   verify_signature)

TICKET = {"id": "T-1", "queue": "support", "reason": "legal threat", "intent": "human_needed",
          "email": "jane@example.com", "message": "my phone is 0917 123 4567 and I will sue"}


def handoff(fake=None, max_attempts=4):
    fake = fake or FakeDiscord()
    sleeps = []
    return DiscordHandoff("tok", "C1", fake, sleep=sleeps.append, max_attempts=max_attempts), fake, sleeps


def desk(*tickets):
    signer, audit = Signer(), []
    d = InteractionDesk(signer.public_hex, audit.append)
    for t in tickets:
        d.register(t)
    return d, signer, audit


def send(d, signer, body, **kw):
    headers, raw = signer.sign(body, **kw)
    return d.handle(headers, raw)


# ---- the message on the wire ---------------------------------------------------------------------------------------------

def test_post_sends_the_documented_url_header_and_body():
    h, fake, _ = handoff()
    r = h.post(TICKET)
    assert r.status == "posted" and r.message_id
    method, url, body = fake.calls[0]
    assert (method, url) == ("POST", f"{API_BASE}/channels/C1/messages")
    assert body["allowed_mentions"] == {"parse": []}
    row = body["components"][0]
    assert row["type"] == 1 and [b["custom_id"] for b in row["components"]] == ["take:T-1", "resolve:T-1"]


def test_the_channel_never_sees_the_customers_words_email_or_phone():
    h, fake, _ = handoff()
    h.post(TICKET)
    text = json.dumps(fake.calls[0][2])
    for secret in ("jane@example.com", "0917", "sue", "message"):
        assert secret not in text


def test_a_mention_in_the_reason_cannot_ping_anyone():
    msg = ticket_message({**TICKET, "reason": "@everyone <@123> look"})
    assert "@" not in msg["content"] and "<" not in msg["content"]


def test_content_is_cut_to_the_documented_limit():
    msg = ticket_message({**TICKET, "reason": "x" * 5000, "intent": "y" * 5000, "queue": "z" * 5000})
    assert len(msg["content"]) <= 2000


def test_the_nonce_fits_25_characters_and_is_stable_per_ticket():
    assert len(nonce_for("T-1")) <= 25 and nonce_for("T-1") == nonce_for("T-1") and nonce_for("T-1") != nonce_for("T-2")
    h, fake, _ = handoff()
    h.post(TICKET)
    body = fake.calls[0][2]
    assert body["nonce"] == nonce_for("T-1") and body["enforce_nonce"] is True


def test_a_second_post_of_the_same_ticket_makes_no_call():
    h, fake, _ = handoff()
    first = h.post(TICKET)
    again = h.post(TICKET)
    assert again.status == "duplicate" and again.message_id == first.message_id and len(fake.calls) == 1


def test_a_lost_answer_is_retried_without_a_second_message():
    h, fake, _ = handoff()
    fake.lose_response()
    r = h.post(TICKET)
    assert r.status == "posted" and r.attempts == 2 and len(fake.messages) == 1


# ---- from a real escalated turn ------------------------------------------------------------------------------------------

def test_an_escalated_turn_becomes_one_channel_message_and_an_answered_turn_becomes_none():
    from agent.core import SupportAgent
    from conftest import FakeShop, ScriptedLLM, TRIAGE_ORDER, order
    from agent.discord_handoff import ticket_from_result

    shop = FakeShop(orders={"1019": ("miguel@example.com", order())})
    legal = SupportAgent(ScriptedLLM(TRIAGE_ORDER), shop, []).handle(
        "I will sue you, my number is 0917 123 4567", "miguel@example.com")
    ticket = ticket_from_result(legal, "conv-42")
    assert ticket and ticket["id"] == "conv-42" and ticket["reason"]
    h, fake, _ = handoff()
    assert h.post(ticket).status == "posted"
    blob = json.dumps(fake.calls)
    assert "0917" not in blob and "miguel@example.com" not in blob and "sue" not in blob.replace("issue", "")
    answered = SupportAgent(ScriptedLLM(TRIAGE_ORDER, ["Your order #1019 is paid and hasn't shipped yet."]), shop, []).handle(
        "Where is order 1019?", "miguel@example.com")
    assert ticket_from_result(answered, "conv-43") is None


# ---- retries ------------------------------------------------------------------------------------------------------------

def test_a_429_waits_the_retry_after_the_body_gives_then_succeeds():
    h, fake, sleeps = handoff()
    fake.script(429, {"message": "You are being rate limited.", "retry_after": 2.5, "global": False})
    r = h.post(TICKET)
    assert r.status == "posted" and sleeps == [2.5]


def test_a_huge_retry_after_is_capped():
    h, fake, sleeps = handoff()
    fake.script(429, {"retry_after": 9999})
    h.post(TICKET)
    assert sleeps == [MAX_RETRY_WAIT]


def test_a_429_without_a_readable_body_waits_one_second():
    h, fake, sleeps = handoff()
    fake.script(429, None)
    h.post(TICKET)
    assert sleeps == [1.0]


def test_a_429_that_never_clears_ends_as_refused_not_a_loop():
    h, fake, _ = handoff(max_attempts=3)
    for _ in range(3):
        fake.script(429, {"retry_after": 1})
    r = h.post(TICKET)
    assert r.status == "refused" and r.code == "rate_limited" and len(fake.calls) == 3
    assert "T-1" not in h.posted


def test_a_5xx_is_retried_with_growing_waits_then_succeeds():
    h, fake, sleeps = handoff()
    fake.script(500, {"message": "oops"})
    fake.script(502, None)
    r = h.post(TICKET)
    assert r.status == "posted" and r.attempts == 3 and sleeps == [0.5, 1.0]


def test_a_5xx_that_never_clears_is_refused():
    h, fake, _ = handoff(max_attempts=2)
    fake.script(500, None)
    fake.script(500, None)
    assert h.post(TICKET).code == "http_500"


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_a_refusal_a_retry_cannot_fix_is_not_retried(status):
    h, fake, sleeps = handoff()
    fake.script(status, {"message": "no", "code": 1})
    r = h.post(TICKET)
    assert r.status == "refused" and r.code == f"http_{status}" and len(fake.calls) == 1 and sleeps == []


def test_a_wrong_token_is_a_401_and_nothing_is_posted():
    fake = FakeDiscord(token="right")
    h = DiscordHandoff("wrong", "C1", fake, sleep=lambda s: None)
    assert h.post(TICKET).code == "http_401" and not fake.messages


def test_a_dropped_connection_is_retried_then_refused_when_it_keeps_dropping():
    h, fake, _ = handoff(max_attempts=3)
    fake.drop(1)
    assert h.post(TICKET).status == "posted"
    h2, fake2, _ = handoff(max_attempts=3)
    fake2.drop(3)
    r = h2.post(TICKET)
    assert r.status == "refused" and r.code == "transport"


# ---- the request signature ----------------------------------------------------------------------------------------------

def test_a_valid_signature_verifies_and_a_changed_byte_does_not():
    s = Signer()
    headers, raw = s.sign({"type": 1})
    assert verify_signature(s.public_hex, headers["X-Signature-Ed25519"], headers["X-Signature-Timestamp"], raw)
    assert not verify_signature(s.public_hex, headers["X-Signature-Ed25519"], headers["X-Signature-Timestamp"], raw + b" ")
    assert not verify_signature(s.public_hex, headers["X-Signature-Ed25519"], "1700000001", raw)
    assert not verify_signature(Signer().public_hex, headers["X-Signature-Ed25519"], headers["X-Signature-Timestamp"], raw)


@pytest.mark.parametrize("sig", ["", "zz", "00" * 10, "00" * 64])
def test_a_malformed_signature_is_false_not_an_exception(sig):
    assert verify_signature(Signer().public_hex, sig, "1", b"{}") is False


def test_a_request_without_both_headers_gets_a_401():
    d, s, _ = desk(TICKET)
    headers, raw = s.sign({"type": 1})
    assert d.handle({"X-Signature-Ed25519": headers["X-Signature-Ed25519"]}, raw)[0] == 401
    assert d.handle({"X-Signature-Timestamp": "1700000000"}, raw)[0] == 401
    assert d.handle({}, raw)[0] == 401


def test_a_forged_click_changes_nothing():
    d, _, audit = desk(TICKET)
    forger = Signer()
    status, _ = send(d, forger, click("take:T-1"))
    assert status == 401 and d.states["T-1"].state == "open" and audit == []


def test_header_names_are_matched_without_regard_to_case():
    d, s, _ = desk(TICKET)
    headers, raw = s.sign({"type": 1})
    assert d.handle({k.lower(): v for k, v in headers.items()}, raw) == (200, {"type": 1})


def test_a_ping_is_answered_with_a_pong():
    d, s, _ = desk()
    assert send(d, s, {"type": 1}) == (200, {"type": 1})


def test_a_signed_body_that_is_not_json_is_a_400():
    d, s, _ = desk()
    headers, raw = s.sign(b"not json")
    assert d.handle(headers, raw)[0] == 400


# ---- the buttons --------------------------------------------------------------------------------------------------------

def test_take_updates_the_message_disables_take_and_records_who():
    d, s, audit = desk(TICKET)
    status, out = send(d, s, click("take:T-1", "u7"))
    assert status == 200 and out["type"] == 7
    take, resolve = out["data"]["components"][0]["components"]
    assert take.get("disabled") is True and not resolve.get("disabled")
    assert audit == [{"discord": "take", "ticket": "T-1", "actor": "u7", "state": "taken"}]


def test_a_second_take_changes_nothing_and_says_who_it_was():
    d, s, audit = desk(TICKET)
    send(d, s, click("take:T-1", "u7"))
    _, same = send(d, s, click("take:T-1", "u7"))
    _, other = send(d, s, click("take:T-1", "u8"))
    assert same["data"]["content"] == "Already taken." and other["data"]["content"] == "Someone else already took it."
    assert same["data"]["flags"] == other["data"]["flags"] == 64
    assert d.states["T-1"].actor == "u7" and len(audit) == 1


def test_resolve_closes_the_ticket_and_every_later_click_is_private():
    d, s, audit = desk(TICKET)
    _, out = send(d, s, click("resolve:T-1", "u7"))
    assert all(b.get("disabled") for b in out["data"]["components"][0]["components"])
    _, later = send(d, s, click("take:T-1", "u8"))
    assert later["data"]["content"] == "Already resolved." and d.states["T-1"].state == "resolved" and len(audit) == 1


def test_an_unknown_ticket_or_button_is_a_private_answer_and_is_audited():
    d, s, audit = desk(TICKET)
    for cid in ("take:T-9", "burn:T-1", "nonsense", ""):
        _, out = send(d, s, click(cid))
        assert out["type"] == 4 and out["data"]["flags"] == 64
    assert d.states["T-1"].state == "open" and len(audit) == 4


def test_a_click_from_a_direct_message_uses_the_user_field():
    d, s, audit = desk(TICKET)
    send(d, s, {"type": 3, "data": {"custom_id": "take:T-1"}, "user": {"id": "dm9"}})
    assert audit[0]["actor"] == "dm9"


def test_the_updated_message_still_names_nobody_to_ping():
    d, s, _ = desk(TICKET)
    _, out = send(d, s, click("take:T-1", "u7"))
    assert out["data"]["allowed_mentions"] == {"parse": []} and "u7" not in out["data"]["content"]


# ---- seeded random worlds ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("seed", range(40))
def test_random_worlds_keep_the_invariants(seed):
    rng = random.Random(seed)
    fake = FakeDiscord()
    sleeps = []
    h = DiscordHandoff("tok", "C1", fake, sleep=sleeps.append, max_attempts=4)
    d, signer, audit = desk()
    tickets = [{"id": f"T-{i}", "queue": rng.choice(["support", "sales"]), "reason": "r" * rng.randint(1, 300),
                "email": f"p{i}@example.com", "message": f"secret words {i}"} for i in range(rng.randint(1, 5))]
    seen_states: dict[str, list[str]] = {t["id"]: ["open"] for t in tickets}
    for t in tickets:
        d.register(t)
    for _ in range(rng.randint(10, 60)):
        t = rng.choice(tickets)
        roll = rng.random()
        if roll < 0.35:
            fault = rng.choice([None, None, "429", "500", "drop", "lose"])
            if fault == "429":
                fake.script(429, {"retry_after": rng.random() * 100})
            elif fault == "500":
                fake.script(500, None)
            elif fault == "drop":
                fake.drop()
            elif fault == "lose":
                fake.lose_response()
            h.post(t)
        else:
            cid = f"{rng.choice(['take', 'resolve', 'take'])}:{t['id']}"
            ts = str(1700000000 + rng.randint(0, 5))
            send(d, signer, click(cid, f"u{rng.randint(1, 3)}"), timestamp=ts)
            if rng.random() < 0.3:      # a replay of the exact same signed request
                send(d, signer, click(cid, "u1"), timestamp=ts)
            seen_states[t["id"]].append(d.states[t["id"]].state)
    per_ticket: dict[str, int] = {}
    for m in fake.messages.values():
        per_ticket[m["content"].split("\n")[0]] = per_ticket.get(m["content"].split("\n")[0], 0) + 1
    assert all(n == 1 for n in per_ticket.values()), "one message per ticket, even through lost answers"
    order = {"open": 0, "taken": 1, "resolved": 2}
    for tid, seq in seen_states.items():
        assert [order[x] for x in seq] == sorted(order[x] for x in seq), "a ticket never moves backwards"
        actors = [a["actor"] for a in audit if a["ticket"] == tid and a["discord"] == "take"]
        assert len(actors) <= 1, "one person takes a ticket"
    changes = sum(1 for a in audit if a.get("state"))
    steps = sum(len({*seq}) - 1 for seq in seen_states.values())
    assert changes >= steps, "every state change is audited"
    assert all(s <= MAX_RETRY_WAIT for s in sleeps)
    blob = json.dumps(fake.calls)
    assert "@example.com" not in blob and "secret words" not in blob
