"""A FAKE Zia Agents service, in process, for the tests and the demo. No network, no account, no real Zia anywhere.

It checks what the saved reference page (docs/vendor-contract/zia-agents-agent-api-reference.txt) says a request carries: the POST to
the documented URL, the three X-ZIAAGENTS headers, `Authorization: Zoho-oauthtoken`, a JSON body with `query` and optionally
`systemArgs`, `reasoning` and `attachments`, and it answers {"data": {"response": "..."}}. Its token endpoint follows the
authentication page.

What it cannot copy is what the pages never show: the shape of an error. A refusal here is a 400 with {"message": ...}, which is an
invention of this file and nothing the client may rely on. An agent is a function `(query, systemArgs) -> str`.
"""
from __future__ import annotations

from typing import Any, Callable

from .zia_agent import TOKEN_URL, ZIA_URL
from .zoho_crm import TransportError
from .zoho_fake import FakeResponse


def echo_agent(query: str, system_args: dict) -> str:
    tools = ", ".join(sorted(system_args)) or "no tools"
    return "You asked: %s (tools: %s)" % (query, tools)


class FakeZia:
    def __init__(self, org: str, agent: str, version: str, answer: Callable[[str, dict], str] = echo_agent):
        self.org, self.agent, self.version, self._answer = org, agent, version, answer
        self.calls: list[tuple[str, str, Any, dict]] = []   # (method, url, body or params, headers)
        self.tokens_issued = 0
        self._valid: str | None = None
        self._drop = 0
        self.refuse_refresh = False  # the refresh token was revoked: the token endpoint answers with an "error" key

    def expire_token(self) -> None:
        self._valid = None

    def drop(self, times: int = 1) -> None:
        self._drop += times

    def request(self, method, url, *, headers, params=None, body=None):
        self.calls.append((method, url, body if body is not None else dict(params or {}), dict(headers)))
        if url == TOKEN_URL:
            return self._token(params or {})
        if self._drop:
            self._drop -= 1
            raise TransportError("timeout")
        if method != "POST" or url != ZIA_URL:
            return FakeResponse(404, {"message": "not found"})
        if headers.get("Authorization") != "Zoho-oauthtoken %s" % self._valid or self._valid is None:
            return FakeResponse(401, {"message": "invalid token"})
        if (headers.get("X-ZIAAGENTS-ORG"), headers.get("X-ZIAAGENTS-AGENT-ID")) != (self.org, self.agent):
            return FakeResponse(400, {"message": "unknown organization or agent"})
        if headers.get("X-ZIAAGENTS-AGENT-VERSION-ID") != self.version:
            return FakeResponse(400, {"message": "stale version id"})  # the page's "check that the version ID ... is up to date"
        if headers.get("Content-Type") != "application/json" or not isinstance(body, dict) or not isinstance(body.get("query"), str):
            return FakeResponse(400, {"message": "bad request"})
        return FakeResponse(200, {"data": {"response": self._answer(body["query"], body.get("systemArgs") or {})}})

    def _token(self, params) -> FakeResponse:
        needed = ("client_id", "client_secret", "refresh_token")
        if self.refuse_refresh or params.get("grant_type") != "refresh_token" or not all(params.get(k) for k in needed):
            return FakeResponse(200, {"error": "invalid_request"})
        self.tokens_issued += 1
        self._valid = "fake-zia-access-%d" % self.tokens_issued
        return FakeResponse(200, {"access_token": self._valid, "expires_in": 3600, "token_type": "Bearer"})
