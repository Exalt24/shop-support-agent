"""WhatsApp lane: a customer's WhatsApp message reaches the support agent and the reply goes back, with a handoff when it escalates.

Written against the pages saved in docs/vendor-contract/whatsapp-*.txt and meta-graph-webhooks-getting-started.txt. Never run
against a live WhatsApp Business account: the tests use the stand-in in agent/whatsapp_fake.py. Each rule names its source.

  Verification (meta-graph-webhooks-getting-started.txt): Meta sends a GET with hub.mode=subscribe, hub.challenge and
    hub.verify_token. "Verify that the hub.verify_token value matches the string you set ... Respond with the hub.challenge
    value." Anything else is refused.
  Signature (same page): every notification carries "X-Hub-Signature-256: sha256={signature}", a SHA256 signature of the
    payload made with the app's App Secret: "Generate a SHA256 signature using the payload and your app's App Secret. Compare
    your signature to the signature in the X-Hub-Signature-256 header (everything after sha256=)." The saved page says
    nothing about how the payload is escaped before signing, so this signs and compares the raw bytes as received. A live
    account may disagree, and that is the first thing a live run confirms.
  Answering (same page): "Your endpoint should respond to all Event Notifications with 200 OK HTTPS." and
    (whatsapp-cloud-set-up-webhooks.txt) "If a webhook request to your endpoint receives an HTTP status code other than 200 ...
    Meta retries delivery with decreasing frequency until the request succeeds, for up to 7 days. ... These retries can result in
    duplicate webhook notifications." So a verified notification is always answered 200 and a message id is handled once.
  Incoming (whatsapp-cloud-webhook-components.txt): entry[].changes[].value.messages[] with from, id (the wamid) and type, and
    text.body for a text message. A value with statuses describes messages we sent and needs no reply.
  Sending (whatsapp-cloud-send-messages.txt): POST https://graph.facebook.com/{version}/{phone_number_id}/messages with
    Authorization: Bearer, and the body {"messaging_product": "whatsapp", "to": ..., "type": "text", "text": {"body": ...}}.
    A free-form service message is allowed inside the 24 hour customer service window that the customer's own message opens,
    and every reply here answers a message that just arrived.

Nothing personal is kept: the audit holds counts and kinds, and a handoff ticket carries a hash of the message id, never the
phone number or the words (see discord_handoff.ticket_from_result).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections import OrderedDict
from typing import Any, Callable, Mapping

from .discord_handoff import DiscordHandoff, ticket_from_result
from .zoho_crm import HttpxTransport, Transport, TransportError

GRAPH_BASE = "https://graph.facebook.com/v27.0"
MAX_BODY = 4096
SEEN_LIMIT = 5000
NON_TEXT_REPLY = "I can read text messages here. Please type your question and I will take a look."
Responder = Callable[[str, str], Any]       # (text, conversation_id) -> core.Result


def verify_handshake(params: Mapping[str, str], verify_token: str) -> tuple[int, str]:
    mode, token, challenge = params.get("hub.mode"), params.get("hub.verify_token", ""), params.get("hub.challenge")
    if mode == "subscribe" and challenge is not None and hmac.compare_digest(token.encode("utf-8"), verify_token.encode("utf-8")):
        return 200, challenge
    return 403, ""


def verify_signature(app_secret: str, header: str | None, raw: bytes) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len("sha256="):].strip().lower())


def conversation_id(sender: str) -> str:
    """Stable per sender, and not the phone number, so the agent's per-conversation state never holds it."""
    return "wa-" + hashlib.sha256(sender.encode("utf-8")).hexdigest()[:16]


def _items(parent: Any, key: str) -> list:
    """parent[key] when it is a list, otherwise nothing: a wrongly typed level is skipped, never an exception."""
    got = parent.get(key) if isinstance(parent, dict) else None
    return got if isinstance(got, list) else []


def _messages(event: Any) -> list[dict]:
    """Every message in a notification, tolerating any missing or wrongly typed level (the page says the shapes vary)."""
    out: list[dict] = []
    for entry in _items(event, "entry"):
        for change in _items(entry, "changes"):
            value = change.get("value") if isinstance(change, dict) else None
            for m in _items(value, "messages"):
                if isinstance(m, dict) and isinstance(m.get("id"), str) and isinstance(m.get("from"), str):
                    out.append(m)
    return out


class WhatsAppDesk:
    def __init__(self, *, app_secret: str, verify_token: str, phone_number_id: str, access_token: str, responder: Responder,
                 http: Transport | None = None, handoff: DiscordHandoff | None = None, audit: Callable[[dict], None] = lambda r: None,
                 base: str = GRAPH_BASE, sleep: Callable[[float], None] = time.sleep, max_attempts: int = 3):
        self._secret, self._verify, self._pid, self._token = app_secret, verify_token, phone_number_id, access_token
        self._responder, self._http, self._handoff, self._audit = responder, http or HttpxTransport(), handoff, audit
        self._base, self._sleep, self._max = base.rstrip("/"), sleep, max_attempts
        self._seen: OrderedDict[str, None] = OrderedDict()

    def handshake(self, params: Mapping[str, str]) -> tuple[int, str]:
        return verify_handshake(params, self._verify)

    def receive(self, headers: Mapping[str, str], raw: bytes) -> tuple[int, dict]:
        h = {k.lower(): v for k, v in headers.items()}
        if not verify_signature(self._secret, h.get("x-hub-signature-256"), raw):
            return 401, {"error": "invalid signature"}
        try:
            event = json.loads(raw)
        except ValueError:
            return 400, {"error": "not json"}
        for m in _messages(event):
            self._one(m)
        return 200, {}        # always 200 after a verified notification: anything else makes Meta retry for 7 days

    def _one(self, m: Mapping[str, Any]) -> None:
        wamid, sender = m["id"], m["from"]
        if wamid in self._seen:
            self._audit({"whatsapp": "duplicate"})
            return
        self._seen[wamid] = None
        while len(self._seen) > SEEN_LIMIT:
            self._seen.popitem(last=False)
        text = (m.get("text") or {}).get("body") if m.get("type") == "text" and isinstance(m.get("text"), dict) else None
        if not isinstance(text, str) or not text.strip():
            self._audit({"whatsapp": "non_text", "type": str(m.get("type", ""))[:20]})
            self._send(sender, NON_TEXT_REPLY)
            return
        cid = conversation_id(sender)
        try:
            res = self._responder(text, cid)
        except Exception:      # a failing agent must not turn into a webhook failure and a week of retries
            self._audit({"whatsapp": "agent_error"})
            return
        sent = self._send(sender, res.reply)
        escalated = getattr(res, "action", None) == "escalated"
        posted = None
        if escalated and self._handoff:
            ticket = ticket_from_result(res, "wa-" + hashlib.sha256(wamid.encode("utf-8")).hexdigest()[:12])
            posted = self._handoff.post(ticket).status if ticket else None
        self._audit({"whatsapp": "handled", "escalated": escalated, "replied": sent, "handoff": posted})

    def _send(self, to: str, body: str) -> bool:
        url = f"{self._base}/{self._pid}/messages"
        headers = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}
        payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": str(body)[:MAX_BODY]}}
        wait = 0.5
        for attempt in range(1, self._max + 1):
            try:
                r = self._http.request("POST", url, headers=headers, body=payload)
            except TransportError:
                r = None
            if r is not None and r.status_code == 200:
                return True
            if r is not None and r.status_code not in (429,) and r.status_code < 500:
                return False                      # a refusal a retry cannot fix (bad token, bad number)
            if attempt < self._max:
                self._sleep(wait)
                wait *= 2
        return False
