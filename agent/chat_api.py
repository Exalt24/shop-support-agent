"""The chat endpoint family behind the embeddable widget (widget/widget.js).

    GET  /api/chat/config?site=<public key>    the tenant's look and greeting, for the launcher
    POST /api/chat/message                     {"site": "<public key>", "message": "...", "conversation_id": "<optional>"}
    GET  /widget/widget.js                     the script itself, so one script tag is the whole install

The request path, in the order the checks run. Each is a separate guard with its own test and its own mutant:
  1. Origin: the browser's Origin header must belong to some tenant, or the request is refused with no CORS headers at all.
  2. Per-IP rate limit, then the body: a Content-Length over the cap is refused unread, and a streamed body is cut off at the cap.
  3. The site key picks the tenant. That is all it does. The request must then come from THAT tenant's origins, so a page on
     site A cannot talk to site B's chat by quoting B's public key.
  4. The conversation id is one this server signed for this tenant, or the request is refused. No id yet: the server issues one.
  5. Per-conversation rate limit, then the agent, which has the tenant's own routing table and Zoho settings.

No cookies are read or set and no credentials are allowed in CORS. Error bodies are fixed codes: nothing the visitor wrote is
echoed back, and the reply is returned as JSON text with control characters removed (the widget draws it as text).
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from starlette.concurrency import run_in_threadpool

from .chat_guard import BodyTooLarge, ConversationSigner, RateLimiter, client_ip, plain_text, read_capped
from .core import SupportAgent
from .tenants import Tenant, TenantRegistry

MAX_BODY_BYTES = 8192
MAX_MESSAGE_CHARS = 1000
WIDGET_JS = Path(__file__).resolve().parent.parent / "widget" / "widget.js"

SAFE_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'"}


class ChatIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    site: str = Field(min_length=1, max_length=80)
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    conversation_id: str | None = Field(default=None, max_length=200)

    @field_validator("message")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("blank")
        return v


class ChatBackend:
    def __init__(self, registry: TenantRegistry, signer: ConversationSigner, agent_for: Callable[[Tenant], SupportAgent], *,
                 trust_proxy: bool = False, ip_per_minute: int = 30, conversation_per_minute: int = 8,
                 config_per_minute: int = 60, now: Callable[[], float] = time.monotonic):
        self.registry, self.signer, self._agent_for = registry, signer, agent_for
        self.trust_proxy = trust_proxy
        self.ip_limit = RateLimiter(ip_per_minute, now=now)
        self.conversation_limit = RateLimiter(conversation_per_minute, now=now)
        self.config_limit = RateLimiter(config_per_minute, now=now)
        self._agents: dict[str, SupportAgent] = {}
        self._lock = threading.Lock()

    def agent(self, tenant: Tenant) -> SupportAgent:
        with self._lock:
            if tenant.id not in self._agents:
                self._agents[tenant.id] = self._agent_for(tenant)
            return self._agents[tenant.id]

    # -- answers -----------------------------------------------------------------------------------------------------------

    @staticmethod
    def _answer(status: int, body: dict, origin: str | None = None, extra: dict | None = None) -> JSONResponse:
        headers = dict(SAFE_HEADERS)
        if origin:  # the exact origin that asked, never "*", and never with credentials
            headers.update({"Access-Control-Allow-Origin": origin, "Vary": "Origin"})
        headers.update(extra or {})
        return JSONResponse(body, status_code=status, headers=headers)

    def _limited(self, retry: int, origin: str) -> JSONResponse:
        return self._answer(429, {"error": "slow_down", "retry_after": retry}, origin, {"Retry-After": str(retry)})

    def _ip(self, request: Request) -> str:
        peer = request.client.host if request.client else None
        return client_ip(peer, request.headers.get("x-forwarded-for"), self.trust_proxy)

    # -- the routes --------------------------------------------------------------------------------------------------------

    def mount(self, app: FastAPI) -> None:
        @app.options("/api/chat/{rest:path}")
        async def preflight(request: Request) -> Response:
            origin = (request.headers.get("origin") or "").lower()
            if not self.registry.origin_known(origin):
                return self._answer(403, {"error": "origin_not_allowed"})
            return Response(status_code=204, headers={**SAFE_HEADERS, "Access-Control-Allow-Origin": origin, "Vary": "Origin",
                                                      "Access-Control-Allow-Methods": "GET, POST",
                                                      "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Max-Age": "600"})

        @app.get("/api/chat/config")
        async def config(request: Request, site: str = "") -> JSONResponse:
            origin = (request.headers.get("origin") or "").lower()
            if not self.registry.origin_known(origin):
                return self._answer(403, {"error": "origin_not_allowed"})
            ok, retry = self.config_limit.hit("ip:" + self._ip(request))
            if not ok:
                return self._limited(retry, origin)
            tenant = self.registry.by_site_key(site)
            if tenant is None:
                return self._answer(404, {"error": "unknown_site"}, origin)
            if origin not in tenant.allowed_origins:
                return self._answer(403, {"error": "origin_not_allowed"}, origin)
            return self._answer(200, tenant.public(), origin)

        @app.post("/api/chat/message")
        async def message(request: Request) -> JSONResponse:
            origin = (request.headers.get("origin") or "").lower()
            if not self.registry.origin_known(origin):
                return self._answer(403, {"error": "origin_not_allowed"})
            ok, retry = self.ip_limit.hit("ip:" + self._ip(request))
            if not ok:
                return self._limited(retry, origin)
            if not (request.headers.get("content-type") or "").lower().startswith("application/json"):
                return self._answer(415, {"error": "json_only"}, origin)
            declared = request.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
                return self._answer(413, {"error": "too_large"}, origin)
            try:
                raw = await read_capped(request.stream(), MAX_BODY_BYTES)
            except BodyTooLarge:
                return self._answer(413, {"error": "too_large"}, origin)
            try:
                req = ChatIn.model_validate(json.loads(raw))
            except (ValueError, ValidationError):  # bad JSON, a missing field, a long message: one fixed answer, nothing echoed
                return self._answer(400, {"error": "bad_request"}, origin)
            tenant = self.registry.by_site_key(req.site)
            if tenant is None:
                return self._answer(404, {"error": "unknown_site"}, origin)
            if origin not in tenant.allowed_origins:
                return self._answer(403, {"error": "origin_not_allowed"}, origin)
            if req.conversation_id is None:
                token = self.signer.issue(tenant.id)
            else:
                token = req.conversation_id
            conversation = self.signer.verify(token, tenant.id)
            if conversation is None:
                return self._answer(400, {"error": "bad_conversation"}, origin)
            ok, retry = self.conversation_limit.hit("%s:%s" % (tenant.id, conversation))
            if not ok:
                return self._limited(retry, origin)
            try:
                # No email: a widget visitor is anonymous. An empty email is never read as the visitor's own address
                # (read_fields checks the shape first), so a lead gets only what the visitor typed.
                result = await run_in_threadpool(self.agent(tenant).handle, req.message, "", conversation)
            except Exception:
                return self._answer(502, {"error": "unavailable"}, origin)
            awaiting = bool(result.enquiry and result.enquiry.awaiting_consent)
            return self._answer(200, {"conversation_id": token, "reply": plain_text(result.reply), "action": result.action,
                                      "awaiting_consent": awaiting}, origin)

        @app.get("/widget/widget.js")
        async def widget_script() -> Response:
            return Response(WIDGET_JS.read_bytes(), media_type="text/javascript",
                            headers={"Cache-Control": "public, max-age=300", "X-Content-Type-Options": "nosniff"})
