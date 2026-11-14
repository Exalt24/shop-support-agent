"""The WhatsApp lane against the in-process FAKE (agent/whatsapp_fake.py). No network, no business account. Each rule is one the saved
vendor pages state, and each has a mutant in mutation_check.py."""
import json
import random

import pytest

from agent.core import SupportAgent
from agent.discord_fake import FakeDiscord
from agent.discord_handoff import DiscordHandoff
from agent.whatsapp_fake import FakeGraph, notification, sign, text_message
from agent.whatsapp_lane import (NON_TEXT_REPLY, WhatsAppDesk, conversation_id, verify_handshake, verify_signature)
from conftest import FakeShop, ScriptedLLM, TRIAGE_ORDER, order

SECRET, VERIFY = "app-secret", "my-verify-token"
LEGAL = "I will sue you. My number is 0917 123 4567"


class Reply:
    def __init__(self, reply, action="replied", escalation_reason=None):
        self.reply, self.action, self.escalation_reason, self.intent = reply, action, escalation_reason, "other"


def desk(responder=None, graph=None, handoff=None, audit=None, max_attempts=3):
    graph = graph or FakeGraph()
    sleeps = []
    d = WhatsAppDesk(app_secret=SECRET, verify_token=VERIFY, phone_number_id="PN1", access_token="tok",
                     responder=responder or (lambda text, cid: Reply("ok: " + text)), http=graph, handoff=handoff,
                     audit=(audit.append if audit is not None else (lambda r: None)), sleep=sleeps.append, max_attempts=max_attempts)
    return d, graph, sleeps


def post(d, raw, secret=SECRET):
    return d.receive(sign(raw, secret), raw)


# ---- the handshake -------------------------------------------------------------------------------------------------------

def test_the_handshake_echoes_the_challenge_for_the_right_token():
    assert verify_handshake({"hub.mode": "subscribe", "hub.verify_token": VERIFY, "hub.challenge": "1158201444"}, VERIFY) == (200, "1158201444")


@pytest.mark.parametrize("params", [
    {"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "1"},
    {"hub.mode": "unsubscribe", "hub.verify_token": VERIFY, "hub.challenge": "1"},
    {"hub.mode": "subscribe", "hub.verify_token": VERIFY},
    {"hub.verify_token": VERIFY, "hub.challenge": "1"},
    {},
])
def test_any_other_handshake_is_refused(params):
    assert verify_handshake(params, VERIFY)[0] == 403


# ---- the signature -------------------------------------------------------------------------------------------------------

def test_a_valid_signature_verifies_and_a_changed_byte_does_not():
    raw = notification(text_message("w1", "639170000001", "hi"))
    assert verify_signature(SECRET, sign(raw, SECRET)["X-Hub-Signature-256"], raw)
    assert not verify_signature(SECRET, sign(raw, SECRET)["X-Hub-Signature-256"], raw + b" ")
    assert not verify_signature(SECRET, sign(raw, "other")["X-Hub-Signature-256"], raw)


@pytest.mark.parametrize("header", [None, "", "sha256=", "sha256=zz", "sha1=" + "0" * 40, "0" * 64])
def test_a_missing_or_malformed_signature_header_is_false(header):
    assert verify_signature(SECRET, header, b"{}") is False


def test_the_hex_in_the_header_may_be_upper_case():
    raw = b'{"a":1}'
    h = sign(raw, SECRET)["X-Hub-Signature-256"]
    assert verify_signature(SECRET, "sha256=" + h[len("sha256="):].upper(), raw)


def test_an_unsigned_or_forged_notification_gets_a_401_and_nothing_is_sent():
    d, g, _ = desk()
    raw = notification(text_message("w1", "639170000001", "hi"))
    assert d.receive({}, raw)[0] == 401
    assert post(d, raw, secret="forged")[0] == 401
    assert g.sent == [] and g.calls == []


def test_header_names_are_matched_without_regard_to_case():
    d, g, _ = desk()
    raw = notification(text_message("w1", "639170000001", "hi"))
    h = {k.lower(): v for k, v in sign(raw, SECRET).items()}
    assert d.receive(h, raw)[0] == 200 and len(g.sent) == 1


def test_a_signed_body_that_is_not_json_is_a_400():
    d, _, _ = desk()
    assert post(d, b"not json")[0] == 400


# ---- a message in, a reply out --------------------------------------------------------------------------------------------

def test_a_text_message_is_answered_to_the_sender_in_the_documented_shape():
    d, g, _ = desk()
    status, _ = post(d, notification(text_message("w1", "639170000001", "where is my order")))
    assert status == 200
    method, url, body = g.calls[0]
    assert (method, url) == ("POST", "https://graph.facebook.com/v27.0/PN1/messages")
    assert body == {"messaging_product": "whatsapp", "to": "639170000001", "type": "text", "text": {"body": "ok: where is my order"}}


def test_the_same_message_id_delivered_again_is_answered_once():
    d, g, _ = desk()
    raw = notification(text_message("w1", "639170000001", "hi"))
    post(d, raw)
    post(d, raw)
    assert len(g.sent) == 1


def test_two_messages_in_one_notification_are_both_answered_to_their_own_sender():
    d, g, _ = desk()
    post(d, notification(text_message("w1", "639170000001", "a"), text_message("w2", "639170000002", "b")))
    assert [(m["to"], m["body"]) for m in g.sent] == [("639170000001", "ok: a"), ("639170000002", "ok: b")]


def test_a_status_only_notification_needs_no_reply():
    d, g, _ = desk()
    status, _ = post(d, notification(statuses=[{"id": "wamid.OUT1", "status": "delivered", "recipient_id": "639170000001"}]))
    assert status == 200 and g.calls == []


def test_a_message_that_is_not_text_gets_the_fixed_reply_and_the_responder_is_not_called():
    called = []
    d, g, _ = desk(responder=lambda t, c: called.append(t) or Reply("x"))
    img = {"from": "639170000001", "id": "w9", "type": "image", "image": {"id": "m1"}}
    post(d, notification(img))
    assert g.sent == [{"to": "639170000001", "body": NON_TEXT_REPLY}] and called == []


@pytest.mark.parametrize("raw", [b"[]", b"{}", b'{"entry": 5}', b'{"entry": [5, null]}', b'{"entry": [{"changes": [null, {"value": 3}]}]}',
                                 b'{"entry": [{"changes": [{"value": {"messages": [1, {"id": 2, "from": 3}, {"id": "a"}]}}]}]}'])
def test_odd_shapes_are_a_200_and_never_an_exception(raw):
    d, g, _ = desk()
    assert post(d, raw)[0] == 200 and g.sent == []


def test_an_empty_text_body_is_treated_as_no_text():
    d, g, _ = desk()
    post(d, notification(text_message("w1", "639170000001", "   ")))
    assert g.sent == [{"to": "639170000001", "body": NON_TEXT_REPLY}]


def test_the_conversation_id_is_stable_per_sender_and_is_not_the_number():
    assert conversation_id("639170000001") == conversation_id("639170000001") != conversation_id("639170000002")
    assert "639170000001" not in conversation_id("639170000001")


def test_a_failing_responder_is_still_a_200_so_meta_does_not_retry_for_a_week():
    def boom(t, c):
        raise RuntimeError("model down")
    d, g, _ = desk(responder=boom)
    assert post(d, notification(text_message("w1", "639170000001", "hi")))[0] == 200 and g.sent == []


def test_a_long_reply_is_cut_to_the_message_limit():
    d, g, _ = desk(responder=lambda t, c: Reply("x" * 10000))
    post(d, notification(text_message("w1", "639170000001", "hi")))
    assert len(g.sent[0]["body"]) == 4096


# ---- sending trouble ------------------------------------------------------------------------------------------------------

def test_a_429_or_5xx_on_send_is_retried_with_growing_waits():
    d, g, sleeps = desk()
    g.script(429, {"error": {"code": 130429}})
    g.script(500, None)
    post(d, notification(text_message("w1", "639170000001", "hi")))
    assert len(g.sent) == 1 and sleeps == [0.5, 1.0]


def test_a_dropped_connection_is_retried():
    d, g, _ = desk()
    g.drop()
    post(d, notification(text_message("w1", "639170000001", "hi")))
    assert len(g.sent) == 1


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_a_refusal_a_retry_cannot_fix_is_not_retried_and_the_webhook_is_still_a_200(status):
    d, g, sleeps = desk()
    g.script(status, {"error": {"code": 1}})
    assert post(d, notification(text_message("w1", "639170000001", "hi")))[0] == 200
    assert len(g.calls) == 1 and sleeps == []


def test_a_send_that_never_works_gives_up_after_the_attempts_and_is_still_a_200():
    d, g, _ = desk(max_attempts=3)
    for _ in range(3):
        g.script(500, None)
    assert post(d, notification(text_message("w1", "639170000001", "hi")))[0] == 200 and len(g.calls) == 3 and g.sent == []


# ---- the audit holds no personal data -----------------------------------------------------------------------------------

def test_the_audit_holds_kinds_and_counts_never_the_number_or_the_words():
    audit = []
    d, _, _ = desk(audit=audit)
    post(d, notification(text_message("w1", "639170000001", "my card is 4111 1111 1111 1111")))
    post(d, notification(text_message("w1", "639170000001", "my card is 4111 1111 1111 1111")))
    blob = json.dumps(audit)
    assert "639170000001" not in blob and "4111" not in blob and "card" not in blob
    assert [a["whatsapp"] for a in audit] == ["handled", "duplicate"]


# ---- escalation reaches a person ----------------------------------------------------------------------------------------

def real_agent_desk(handoff=None, audit=None):
    shop = FakeShop(orders={"1019": ("miguel@example.com", order())})
    agent = SupportAgent(ScriptedLLM(TRIAGE_ORDER), shop, [])
    return desk(responder=lambda text, cid: agent.handle(text, "", conversation_id=cid), handoff=handoff, audit=audit)


def test_a_legal_threat_gets_the_holding_reply_and_one_staff_ticket_even_when_meta_retries():
    discord = FakeDiscord()
    handoff = DiscordHandoff("tok", "C1", discord, sleep=lambda s: None)
    audit = []
    d, g, _ = real_agent_desk(handoff, audit)
    raw = notification(text_message("w-legal", "639170000001", LEGAL))
    post(d, raw)
    post(d, raw)
    assert len(g.sent) == 1 and len(discord.messages) == 1
    blob = json.dumps(discord.calls) + json.dumps(audit)
    assert "0917" not in blob and "639170000001" not in blob and "sue" not in blob.replace("issue", "")
    assert audit[0]["escalated"] is True and audit[0]["handoff"] == "posted"


def test_an_escalation_without_a_handoff_still_replies():
    d, g, _ = real_agent_desk()
    post(d, notification(text_message("w-legal", "639170000001", LEGAL)))
    assert len(g.sent) == 1


# ---- seeded random worlds -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("seed", range(40))
def test_random_worlds_keep_the_invariants(seed):
    rng = random.Random(seed)
    graph, discord = FakeGraph(), FakeDiscord()
    handoff = DiscordHandoff("tok", "C1", discord, sleep=lambda s: None)
    audit = []

    def responder(text, cid):
        return Reply("holding", "escalated", "rule") if "sue" in text else Reply("ok " + text)

    d, _, _ = desk(responder=responder, graph=graph, handoff=handoff, audit=audit)
    senders = [f"6391700000{i:02d}" for i in range(rng.randint(1, 4))]
    ids = [f"w{i}" for i in range(rng.randint(3, 12))]
    expected: dict[str, str] = {}
    for _ in range(rng.randint(10, 50)):
        wamid = rng.choice(ids)
        sender = expected.get(wamid) or rng.choice(senders)
        expected[wamid] = sender
        text = rng.choice(["hello there", "I will sue", "order 1019?", "secret " + wamid])
        if rng.random() < 0.3:
            graph.script(rng.choice([429, 500, 400]), None)
        if rng.random() < 0.2:
            graph.drop()
        raw = notification(text_message(wamid, sender, text))
        secret = SECRET if rng.random() > 0.15 else "forged"
        status, _ = post(d, raw, secret)
        assert status == (200 if secret == SECRET else 401)
    handled = [a for a in audit if a["whatsapp"] in ("handled", "non_text")]
    assert len(handled) <= len(ids), "each message id is handled at most once"
    tickets = [m["content"].split("\n")[0] for m in discord.messages.values()]
    assert len(tickets) == len(set(tickets)), "one staff ticket per escalated message"
    assert all(m["to"] in senders for m in graph.sent), "a reply only ever goes to a real sender"
    blob = json.dumps(discord.calls) + json.dumps(audit)
    assert not any(s in blob for s in senders) and "secret w" not in blob
