"""A client for the Zia Agents "Agent API": send a query to a deployed Zia agent and read its answer. Never run against a live
account; tests and the demo use agent/zia_fake.py.

Written only from the two pages saved in docs/vendor-contract/ (fetched 2026-10-30), and each rule quotes its line:

  Endpoint (zia-agents-agent-api-reference.txt): "POST https://ziaagents.zoho.com/ziaagents/api/v1/agents/query".
  Headers: "X-ZIAAGENTS-ORG" (the organization id), "X-ZIAAGENTS-AGENT-ID", "X-ZIAAGENTS-AGENT-VERSION-ID" ("The version ID changes
    each time you redeploy the agent. If your API calls start failing after a redeployment, check that the version ID in your
    headers is up to date."), "Authorization: Zoho-oauthtoken {access_token}" and "Content-Type: application/json".
  Body: "query" (String, the message or prompt), "systemArgs" (Object, "Key-value pairs that map to the agent's tool parameters"),
    "reasoning" (Boolean, optional), "attachments" (Array of attachment IDs, optional).
  Answer: {"data": {"response": "..."}}.
  Auth (zia-agents-api-overview-and-authentication.txt): OAuth 2.0 with a refresh token ("The refresh token is permanent"); a new
    access token from "POST https://accounts.zoho.com/oauth/v2/token" with client_id, client_secret, refresh_token and
    grant_type=refresh_token; "valid for one hour". The scope the grant needs is the agent's own, shown on its Integrate tab
    ("The OAuth scope for the Agent API is ZiaAgents.agents.TRIGGER"). Getting the grant token and the first refresh token is a
    one-time step in the Zoho API Console, done by a person, and is not in this client.

What the pages do NOT say, so the client does not guess: the shape of an error body, any status code other than success, a rate
limit, a retry rule, a size limit for query or systemArgs, a data centre other than .com, a streaming form, and whether a 401 is how
an expired token shows up. So: no retries, no other data centres, and any answer that is not a 2xx with data.response is raised as
a ZiaError carrying the HTTP status and a short scrubbed excerpt. The size limits below are this repo's own, chosen to keep an
agent from posting a book, and are not Zia's.

The answer is model-written text. It is returned as a string and nothing here reads it as a command.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

from .zoho_crm import HttpxTransport, Transport, TransportError, scrub

ZIA_URL = "https://ziaagents.zoho.com/ziaagents/api/v1/agents/query"
TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"
TOKEN_SKEW_SECONDS = 60
DEFAULT_EXPIRES_IN = 3600  # "valid for one hour"
MAX_QUERY_CHARS = 4000
MAX_SYSTEM_ARGS_CHARS = 8000
MAX_ATTACHMENTS = 10
MAX_ANSWER_CHARS = 8000
ID_OK = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")  # ids in the docs are digit strings; this also keeps a CR or LF out of a header


class ZiaError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, code: str = "ZIA_ERROR"):
        super().__init__(message)
        self.status, self.code = status, code


@dataclass(frozen=True)
class ZiaSettings:
    """From the environment only: ZIA_CLIENT_ID, ZIA_CLIENT_SECRET, ZIA_REFRESH_TOKEN, ZIA_ORG_ID, ZIA_AGENT_ID,
    ZIA_AGENT_VERSION_ID (the last three are on the agent's Integrate tab)."""
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    refresh_token: str = field(repr=False)
    org_id: str
    agent_id: str
    agent_version_id: str

    def __post_init__(self):
        for name in ("org_id", "agent_id", "agent_version_id"):
            if not ID_OK.match(getattr(self, name) or ""):
                raise ValueError("%s must be 1 to 64 letters, digits, hyphens or underscores" % name)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ZiaSettings":
        env = os.environ if env is None else env
        names = ("ZIA_CLIENT_ID", "ZIA_CLIENT_SECRET", "ZIA_REFRESH_TOKEN", "ZIA_ORG_ID", "ZIA_AGENT_ID", "ZIA_AGENT_VERSION_ID")
        missing = [n for n in names if not env.get(n)]
        if missing:
            raise RuntimeError("missing configuration: " + ", ".join(missing))
        return cls(*(env[n] for n in names))


@dataclass(frozen=True)
class AgentAnswer:
    response: str
    truncated: bool = False


class AgentApi(Protocol):
    """What a caller needs from an agent service. ZiaAgentClient is the one real implementation; tests inject a fake."""
    def query(self, query: str, system_args: dict | None = None, *, reasoning: bool = False,
              attachments: list[str] | None = None) -> AgentAnswer: ...


def _scalar(v: object) -> bool:
    return v is None or isinstance(v, (str, int, float, bool))


def check_system_args(system_args: object) -> dict:
    """`systemArgs` is {tool name: {parameter: value}}. Two levels, scalar values, and a size cap."""
    if not isinstance(system_args, dict):
        raise ValueError("system_args must be an object of tool names")
    for tool, params in system_args.items():
        if not isinstance(tool, str) or not isinstance(params, dict) or not all(isinstance(k, str) and _scalar(v) for k, v in params.items()):
            raise ValueError("system_args must map a tool name to an object of plain values")
    if len(json.dumps(system_args)) > MAX_SYSTEM_ARGS_CHARS:
        raise ValueError("system_args is over %d characters" % MAX_SYSTEM_ARGS_CHARS)
    return system_args


class ZiaAgentClient:
    def __init__(self, settings: ZiaSettings, transport: Transport | None = None, *, clock: Callable[[], float] = time.time):
        self._s = settings
        self._transport = transport or HttpxTransport()
        self._clock = clock
        self._token: str | None = None
        self._expires_at = 0.0

    def _access_token(self) -> str:
        if self._token and self._clock() < self._expires_at:
            return self._token
        try:
            r = self._transport.request("POST", TOKEN_URL, headers={}, params={
                "client_id": self._s.client_id, "client_secret": self._s.client_secret, "refresh_token": self._s.refresh_token,
                "grant_type": "refresh_token"})
        except TransportError as e:
            raise ZiaError("token refresh: transport error %s" % e, code="TRANSPORT") from e
        try:
            body = r.json()
        except ValueError:
            body = {}
        token = body.get("access_token") if isinstance(body, dict) else None
        if r.status_code != 200 or not isinstance(token, str) or not token:
            why = body.get("error") if isinstance(body, dict) else None
            raise ZiaError("token refresh refused: %s" % scrub(why or "HTTP %s" % r.status_code), status=r.status_code, code="TOKEN_REFRESH_FAILED")
        try:
            lifetime = int(body.get("expires_in") or DEFAULT_EXPIRES_IN)
        except (TypeError, ValueError):
            lifetime = DEFAULT_EXPIRES_IN
        self._token, self._expires_at = token, self._clock() + max(lifetime - TOKEN_SKEW_SECONDS, 0)
        return token

    def query(self, query: str, system_args: dict | None = None, *, reasoning: bool = False,
              attachments: list[str] | None = None) -> AgentAnswer:
        if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_CHARS:
            raise ValueError("query must be text of 1 to %d characters" % MAX_QUERY_CHARS)
        body: dict[str, Any] = {"query": query}
        if system_args is not None:
            body["systemArgs"] = check_system_args(system_args)
        if reasoning:
            body["reasoning"] = True
        if attachments:
            if len(attachments) > MAX_ATTACHMENTS or not all(isinstance(a, str) and ID_OK.match(a) for a in attachments):
                raise ValueError("attachments must be up to %d attachment ids" % MAX_ATTACHMENTS)
            body["attachments"] = list(attachments)
        token = self._access_token()
        try:
            r = self._transport.request("POST", ZIA_URL, headers={
                "X-ZIAAGENTS-ORG": self._s.org_id, "X-ZIAAGENTS-AGENT-ID": self._s.agent_id,
                "X-ZIAAGENTS-AGENT-VERSION-ID": self._s.agent_version_id,
                "Authorization": "Zoho-oauthtoken " + token, "Content-Type": "application/json"}, body=body)
        except TransportError as e:
            raise ZiaError("transport error %s" % e, code="TRANSPORT") from e
        if not 200 <= r.status_code < 300:
            if r.status_code == 401:
                self._token = None  # the next call fetches a fresh one; this one is not retried (the pages do not say a 401 means expiry)
            raise ZiaError("the agent API answered HTTP %d: %s" % (r.status_code, scrub(getattr(r, "text", "") or "no body")),
                           status=r.status_code, code="HTTP_%d" % r.status_code)
        try:
            data = r.json().get("data")
        except (ValueError, AttributeError):
            data = None
        text = data.get("response") if isinstance(data, dict) else None
        if not isinstance(text, str):
            raise ZiaError("the answer has no data.response", status=r.status_code, code="BAD_RESPONSE")
        return AgentAnswer(text[:MAX_ANSWER_CHARS], truncated=len(text) > MAX_ANSWER_CHARS)
