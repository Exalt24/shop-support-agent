"""Discord handoff: an escalated turn becomes one message in a staff channel with Take and Resolve buttons.

Written against the pages saved in docs/vendor-contract/discord-*.txt. Never run against a live Discord server: the tests use
the stand-in in agent/discord_fake.py. Each rule below names the saved line it comes from.

  Create Message (discord-resource-message.txt): `content` is "up to 2000 characters"; `nonce` is "up to 25 characters" and with
    `enforce_nonce` true "If another message was created by the same author with the same nonce, that message will be returned
    and no new message will be created", so a retried post cannot double up. `allowed_mentions` decides who is notified.
  Rate limits (discord-rate-limits.txt): "the API will return a HTTP 429 response code with a JSON body. Your application should
    rely on the Retry-After header or retry_after field to determine when to retry the request." 401 and 403 are not retried.
  Components (discord-components-reference.txt): a button sits in an Action Row (type 1, "up to 5 Buttons"); "Non-link and
    non-premium buttons must have a custom_id" of 1 to 100 characters; styles Primary 1, Success 3, Danger 4.
  Interactions (discord-interactions-overview.txt): each request carries X-Signature-Ed25519 and X-Signature-Timestamp,
    "If the signature fails validation, your app should respond with a 401 error code", and a PING (type 1) is acknowledged with
    a PONG (type 1). The saved page does not print the signed bytes. The code signs and verifies timestamp followed by the raw
    body, which is Discord's published example; the day a live Discord answers differently, this is the rule to fix.
  Responding (discord-interactions-receiving-and-responding.txt): a message component click is type 3; the answer is type 7
    (UPDATE_MESSAGE, edit the message the button was on) or type 4 (a new message, EPHEMERAL when the flags carry 64); an initial
    response is due "within 3 seconds", so nothing here waits on a network call.

What goes into the channel is the ticket id, the queue, the reason and the intent. Never the customer's words, email, phone or order
details: a staff channel is a chat room, and personal data stays in the helpdesk it points to.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .zoho_crm import HttpxTransport, Transport, TransportError

API_BASE = "https://discord.com/api/v10"
MAX_NONCE = 25
MAX_RETRY_WAIT = 30.0
EPHEMERAL = 64
PING, MESSAGE_COMPONENT = 1, 3
PONG, CHANNEL_MESSAGE, UPDATE_MESSAGE = 1, 4, 7
ACTION_ROW, BUTTON = 1, 2
STYLE_PRIMARY, STYLE_SUCCESS = 1, 3
NO_MENTIONS = {"parse": []}


class DiscordError(RuntimeError):
    """Discord refused the post for a reason a retry cannot fix (bad token, no access, bad payload)."""

    def __init__(self, status: int, code: str):
        super().__init__(f"discord {status} {code}")
        self.status, self.code = status, code


def nonce_for(ticket_id: str) -> str:
    """Stable per ticket, so a retry carries the same nonce and Discord returns the first message."""
    return hashlib.sha256(ticket_id.encode("utf-8")).hexdigest()[:MAX_NONCE]


def _clean(text: str, limit: int) -> str:
    """One line, no mention syntax, cut to a limit. Ticket fields are ours, but a reason string can quote a rule name."""
    flat = " ".join(str(text).split()).replace("@", "at ").replace("<", "(").replace(">", ")")
    return flat[:limit]


def buttons(ticket_id: str, state: str = "open") -> list[dict]:
    take = {"type": BUTTON, "style": STYLE_PRIMARY, "label": "Take", "custom_id": f"take:{ticket_id}"}
    done = {"type": BUTTON, "style": STYLE_SUCCESS, "label": "Resolve", "custom_id": f"resolve:{ticket_id}"}
    if state in ("taken", "resolved"):
        take["disabled"] = True
    if state == "resolved":
        done["disabled"] = True
    return [{"type": ACTION_ROW, "components": [take, done]}]


def ticket_message(ticket: Mapping[str, Any], state: str = "open", note: str = "") -> dict:
    tid = _clean(ticket["id"], 40)
    lines = [f"Handoff {tid}", f"Queue: {_clean(ticket.get('queue', 'support'), 40)}",
             f"Why: {_clean(ticket.get('reason', 'needs a person'), 120)}"]
    if ticket.get("intent"):
        lines.append(f"Intent: {_clean(ticket['intent'], 40)}")
    if note:
        lines.append(_clean(note, 120))
    # Every field is capped on its own above, so the whole message stays far under the documented 2000 characters.
    return {"content": "\n".join(lines), "allowed_mentions": NO_MENTIONS, "components": buttons(tid, state)}


def ticket_from_result(res: Any, ticket_id: str) -> dict | None:
    """The handoff ticket for an escalated turn (core.Result), or None for a turn the agent answered itself.

    Only the rule that fired and the intent go in. The customer's message, email and order stay with the helpdesk."""
    if getattr(res, "action", None) != "escalated":
        return None
    enquiry = getattr(res, "enquiry", None)
    return {"id": ticket_id, "queue": getattr(enquiry, "queue", None) or "support",
            "reason": res.escalation_reason or "needs a person", "intent": res.intent}


@dataclass
class PostResult:
    status: str                      # "posted", "duplicate" or "refused"
    message_id: str | None = None
    attempts: int = 0
    code: str | None = None


class DiscordHandoff:
    """Posts one handoff message per ticket. The ledger is in memory (one process), like the rest of the agent."""

    def __init__(self, token: str, channel_id: str, http: Transport | None = None, *, base: str = API_BASE,
                 sleep: Callable[[float], None] = time.sleep, max_attempts: int = 4):
        self._token, self._channel, self._base = token, channel_id, base.rstrip("/")
        self._http = http or HttpxTransport()
        self._sleep, self._max = sleep, max_attempts
        self.posted: dict[str, str] = {}

    def post(self, ticket: Mapping[str, Any]) -> PostResult:
        tid = str(ticket["id"])
        if tid in self.posted:
            return PostResult("duplicate", self.posted[tid])
        body = {**ticket_message(ticket), "nonce": nonce_for(tid), "enforce_nonce": True}
        headers = {"Authorization": f"Bot {self._token}", "Content-Type": "application/json",
                   "User-Agent": "DiscordBot (https://github.com/Exalt24/shop-support-agent, 1)"}
        url = f"{self._base}/channels/{self._channel}/messages"
        wait = 0.5
        for attempt in range(1, self._max + 1):
            try:
                r = self._http.request("POST", url, headers=headers, body=body)
            except TransportError:
                if attempt == self._max:
                    return PostResult("refused", None, attempt, "transport")
                self._sleep(wait); wait *= 2
                continue
            if r.status_code == 200:
                mid = str(r.json().get("id"))
                self.posted[tid] = mid
                return PostResult("posted", mid, attempt)
            if r.status_code == 429:
                if attempt == self._max:
                    return PostResult("refused", None, attempt, "rate_limited")
                self._sleep(min(self._retry_after(r), MAX_RETRY_WAIT))
                continue
            if r.status_code >= 500:
                if attempt == self._max:
                    return PostResult("refused", None, attempt, f"http_{r.status_code}")
                self._sleep(wait); wait *= 2
                continue
            return PostResult("refused", None, attempt, f"http_{r.status_code}")   # 400, 401, 403, 404: a retry cannot fix it
        return PostResult("refused", None, self._max, "exhausted")

    @staticmethod
    def _retry_after(r) -> float:
        try:
            return max(0.0, float(r.json()["retry_after"]))
        except (ValueError, KeyError, TypeError):
            return 1.0


def verify_signature(public_key_hex: str, signature_hex: str, timestamp: str, body: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex)).verify(
            bytes.fromhex(signature_hex), timestamp.encode("utf-8") + body)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


@dataclass
class TicketState:
    state: str = "open"              # open, taken, resolved
    actor: str | None = None


@dataclass
class InteractionDesk:
    """The endpoint a Discord application points at. One click changes one ticket once; a repeat click changes nothing."""
    public_key_hex: str
    audit: Callable[[dict], None] = lambda record: None
    on_change: Callable[[str, str, str], None] = lambda ticket_id, state, actor: None   # e.g. move a Notion row's Status
    tickets: dict[str, Mapping[str, Any]] = field(default_factory=dict)
    states: dict[str, TicketState] = field(default_factory=dict)

    def register(self, ticket: Mapping[str, Any]) -> None:
        self.tickets[str(ticket["id"])] = dict(ticket)
        self.states.setdefault(str(ticket["id"]), TicketState())

    def handle(self, headers: Mapping[str, str], body: bytes) -> tuple[int, dict]:
        h = {k.lower(): v for k, v in headers.items()}
        sig, ts = h.get("x-signature-ed25519"), h.get("x-signature-timestamp")
        if not sig or not ts or not verify_signature(self.public_key_hex, sig, ts, body):
            return 401, {"error": "invalid request signature"}
        try:
            event = json.loads(body)
        except ValueError:
            return 400, {"error": "not json"}
        if event.get("type") == PING:
            return 200, {"type": PONG}
        if event.get("type") != MESSAGE_COMPONENT:
            return 200, self._private("Not a button this desk handles.")
        return 200, self._click(event)

    def _click(self, event: Mapping[str, Any]) -> dict:
        custom_id = str((event.get("data") or {}).get("custom_id", ""))
        action, _, tid = custom_id.partition(":")
        who = str(((event.get("member") or {}).get("user") or event.get("user") or {}).get("id", "unknown"))
        st = self.states.get(tid)
        if action not in ("take", "resolve") or st is None:
            self.audit({"discord": "unknown_button", "ticket": tid[:40]})
            return self._private("That ticket is not known here.")
        if st.state == "resolved":
            return self._private("Already resolved.")
        if action == "take":
            if st.state == "taken":
                return self._private("Already taken." if st.actor == who else "Someone else already took it.")
            st.state, st.actor = "taken", who
        else:
            st.state = "resolved"
        self.audit({"discord": action, "ticket": tid[:40], "actor": who, "state": st.state})
        try:
            self.on_change(tid, st.state, who)       # a failing downstream log must never turn a click into an error
        except Exception:
            self.audit({"discord": "on_change_failed", "ticket": tid[:40]})
        note = "Taken by a staff member." if st.state == "taken" else "Resolved."
        return {"type": UPDATE_MESSAGE, "data": ticket_message(self.tickets[tid], st.state, note)}

    @staticmethod
    def _private(text: str) -> dict:
        return {"type": CHANNEL_MESSAGE, "data": {"content": text, "flags": EPHEMERAL, "allowed_mentions": NO_MENTIONS}}
