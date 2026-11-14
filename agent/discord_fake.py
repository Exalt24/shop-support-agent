"""A FAKE Discord, in process, for the tests. No network, no bot, no server, no Discord anywhere.

It follows the shapes on the pages saved in docs/vendor-contract/discord-*.txt: the Bot authorization header, Create Message with
its 2000 character and 25 character nonce limits, `enforce_nonce` returning the first message for a repeated nonce, an Action
Row of at most 5 buttons that each need a custom_id of 1 to 100 characters, and a 429 with a retry_after body. It is NOT
Discord: it knows no permissions, intents or gateway, and the day a live server answers differently, this file was wrong.

Faults are scripted: `script(status, body)` makes the next call answer that way, `drop()` makes the next call fail in
transport before Discord sees it, and `lose_response()` lets Discord create the message and then loses the answer on the
way back, which is the case a nonce exists for.
"""
from __future__ import annotations

import json
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from .zoho_crm import TransportError
from .zoho_fake import FakeResponse


class FakeDiscord:
    def __init__(self, token: str = "tok", channel: str = "C1"):
        self._token, self._channel = token, channel
        self.messages: dict[str, dict] = {}
        self.calls: list[tuple[str, str, Any]] = []
        self._scripted: list[tuple[int, Any]] = []
        self._drop = 0
        self._lose = 0
        self._by_nonce: dict[str, str] = {}
        self._next = 1000

    def script(self, status: int, body: Any = None) -> None:
        self._scripted.append((status, body))

    def drop(self, times: int = 1) -> None:
        self._drop += times

    def lose_response(self, times: int = 1) -> None:
        self._lose += times

    def request(self, method, url, *, headers, params=None, body=None):
        self.calls.append((method, url, body))
        if self._drop:
            self._drop -= 1
            raise TransportError("ConnectionReset")
        if self._scripted:
            status, payload = self._scripted.pop(0)
            return FakeResponse(status, payload)
        if headers.get("Authorization") != f"Bot {self._token}":
            return FakeResponse(401, {"message": "401: Unauthorized", "code": 0})
        if method != "POST" or not url.endswith(f"/channels/{self._channel}/messages"):
            return FakeResponse(404, {"message": "Unknown Channel", "code": 10003})
        problem = self._invalid(body or {})
        if problem:
            return FakeResponse(400, {"message": problem, "code": 50035})
        nonce = body.get("nonce")
        if nonce is not None and body.get("enforce_nonce") and str(nonce) in self._by_nonce:
            first = self._by_nonce[str(nonce)]
            return FakeResponse(200, self.messages[first])
        self._next += 1
        mid = str(self._next)
        self.messages[mid] = {"id": mid, "channel_id": self._channel, "content": body.get("content", ""),
                              "components": body.get("components", [])}
        if nonce is not None:
            self._by_nonce[str(nonce)] = mid
        if self._lose:
            self._lose -= 1
            raise TransportError("ReadTimeout")
        return FakeResponse(200, self.messages[mid])

    @staticmethod
    def _invalid(body: dict) -> str:
        if len(body.get("content", "")) > 2000:
            return "content: Must be 2000 or fewer in length."
        if "nonce" in body and len(str(body["nonce"])) > 25:
            return "nonce: Must be 25 or fewer in length."
        for row in body.get("components", []):
            kids = row.get("components", [])
            if row.get("type") != 1 or not 1 <= len(kids) <= 5:
                return "components: an Action Row holds 1 to 5 buttons."
            for b in kids:
                if b.get("type") == 2 and b.get("style") != 5 and not 1 <= len(str(b.get("custom_id", ""))) <= 100:
                    return "custom_id: a button needs a custom_id of 1 to 100 characters."
        return ""


class Signer:
    """The application's key pair, as Discord holds it: the public half goes into the desk, the private half signs requests."""

    def __init__(self):
        self._key = Ed25519PrivateKey.generate()
        self.public_hex = self._key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()

    def sign(self, body: dict | bytes, timestamp: str = "1700000000") -> tuple[dict, bytes]:
        raw = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        sig = self._key.sign(timestamp.encode("utf-8") + raw).hex()
        return {"X-Signature-Ed25519": sig, "X-Signature-Timestamp": timestamp}, raw


def click(custom_id: str, user: str = "u1") -> dict:
    """A message component interaction as the saved page describes it: type 3, data.custom_id, member.user.id."""
    return {"type": 3, "data": {"custom_id": custom_id, "component_type": 2}, "member": {"user": {"id": user}}}
