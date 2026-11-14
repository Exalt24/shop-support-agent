"""A FAKE WhatsApp Cloud API, in process, for the tests. No network, no business account, no phone number, no Meta anywhere.

It follows the shapes on the pages saved in docs/vendor-contract/whatsapp-*.txt and meta-graph-webhooks-getting-started.txt:
the Bearer header, the send body (messaging_product, to, type, text.body), the notification structure
(entry, changes, value, messages with from, id, type, text.body) and the X-Hub-Signature-256 header made from the App Secret.
It is NOT WhatsApp: it knows no customer service window, templates or quality ratings, and the day a live account answers
differently, this file was wrong.

Faults are scripted: `script(status, body)` makes the next send answer that way and `drop()` fails the next send in transport.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from .zoho_crm import TransportError
from .zoho_fake import FakeResponse


class FakeGraph:
    def __init__(self, token: str = "tok", phone_number_id: str = "PN1"):
        self._token, self._pid = token, phone_number_id
        self.sent: list[dict] = []
        self.calls: list[tuple[str, str, Any]] = []
        self._scripted: list[tuple[int, Any]] = []
        self._drop = 0
        self._n = 0

    def script(self, status: int, body: Any = None) -> None:
        self._scripted.append((status, body))

    def drop(self, times: int = 1) -> None:
        self._drop += times

    def request(self, method, url, *, headers, params=None, body=None):
        self.calls.append((method, url, body))
        if self._drop:
            self._drop -= 1
            raise TransportError("ConnectionReset")
        if self._scripted:
            status, payload = self._scripted.pop(0)
            return FakeResponse(status, payload)
        if headers.get("Authorization") != f"Bearer {self._token}":
            return FakeResponse(401, {"error": {"message": "Invalid OAuth access token.", "code": 190}})
        if method != "POST" or not url.endswith(f"/{self._pid}/messages"):
            return FakeResponse(404, {"error": {"message": "Unsupported post request.", "code": 100}})
        b = body or {}
        text = (b.get("text") or {}).get("body")
        if b.get("messaging_product") != "whatsapp" or b.get("type") != "text" or not b.get("to") or not isinstance(text, str) \
                or not 1 <= len(text) <= 4096:
            return FakeResponse(400, {"error": {"message": "Invalid parameter", "code": 100}})
        self._n += 1
        self.sent.append({"to": b["to"], "body": text})
        return FakeResponse(200, {"messaging_product": "whatsapp", "contacts": [{"input": b["to"], "wa_id": b["to"]}],
                                  "messages": [{"id": f"wamid.OUT{self._n}"}]})


def sign(body: bytes, secret: str) -> dict:
    return {"X-Hub-Signature-256": "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()}


def notification(*messages: dict, statuses: list | None = None) -> bytes:
    """One webhook notification with the given messages (and optionally status updates), as the saved pages show it."""
    value: dict = {"messaging_product": "whatsapp", "metadata": {"display_phone_number": "15550001111", "phone_number_id": "PN1"}}
    if messages:
        value["messages"] = list(messages)
    if statuses:
        value["statuses"] = statuses
    return json.dumps({"object": "whatsapp_business_account",
                       "entry": [{"id": "WABA1", "changes": [{"value": value, "field": "messages"}]}]}).encode("utf-8")


def text_message(wamid: str, sender: str, body: str) -> dict:
    return {"from": sender, "id": wamid, "timestamp": "1749416383", "type": "text", "text": {"body": body}}
