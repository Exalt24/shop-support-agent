"""A FAKE Zoho CRM, in process, for the tests and the demo. No network, no account, no real Zoho anywhere.

It follows the shapes on the vendor pages saved in docs/vendor-contract/ (the token answer, the Authorization header, the
upsert answer with "action", the SUCCESS records, the error codes), so the client is exercised against the documented
contract. It is NOT Zoho: it does not know field types, layouts, workflows or the real rate limits, and the day a real
account answers differently, this file is what was wrong.

Faults are scripted: `script(status, body, on="Tasks")` queues a response that the next CRM call (whose path contains `on`,
if given) receives instead of the normal one,
`expire_token()` makes the current access token stop working the way an expired one does, `drop()` makes the next call fail
in transport.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from .zoho_crm import REGIONS, TransportError


class FakeResponse:
    def __init__(self, status_code: int, body: Any):
        self.status_code, self._body = status_code, body
        self.text = "" if body is None else str(body)

    def json(self) -> Any:
        if self._body is None:
            raise ValueError("no body")
        return self._body


def _ok(**details) -> dict:
    return {"code": "SUCCESS", "details": {"id": details.pop("id"), **details}, "message": "record added", "status": "success"}


class FakeZoho:
    """Implements the Transport protocol. `calls` is every request it saw, as (method, url, body)."""

    def __init__(self, region: str = "us"):
        self.accounts_host = urlparse(REGIONS[region][0]).netloc
        self.api_host = urlparse(REGIONS[region][1]).netloc
        self.calls: list[tuple[str, str, Any]] = []
        self.leads: dict[str, dict] = {}   # id -> record
        self.tasks: list[dict] = []
        self.notes: list[dict] = []
        self.tokens_issued = 0
        self._valid: str | None = None
        self._scripted: list[tuple[int, Any, str | None]] = []
        self._drop = 0
        self._next_id = 1000

    # -- fault scripting --------------------------------------------------------------------------------------------

    def script(self, status: int, body: Any = None, on: str | None = None) -> None:
        self._scripted.append((status, body, on))

    def expire_token(self) -> None:
        self._valid = None

    def drop(self, times: int = 1) -> None:
        self._drop += times

    # -- helpers ----------------------------------------------------------------------------------------------------

    def _id(self) -> str:
        self._next_id += 1
        return str(self._next_id)

    def api_calls(self) -> list[tuple[str, str, Any]]:
        return [c for c in self.calls if urlparse(c[1]).netloc == self.api_host]

    def token_calls(self) -> list[tuple[str, str, Any]]:
        return [c for c in self.calls if urlparse(c[1]).netloc == self.accounts_host]

    # -- the Transport protocol -------------------------------------------------------------------------------------

    def request(self, method, url, *, headers, params=None, body=None):
        self.calls.append((method, url, body if body is not None else dict(params or {})))
        host, path = urlparse(url).netloc, urlparse(url).path
        if host == self.accounts_host and path == "/oauth/v2/token":
            return self._token(params or {})
        if host != self.api_host:
            return FakeResponse(404, {"code": "INVALID_URL_PATTERN", "message": "wrong data centre for this account", "status": "error"})
        if self._drop:
            self._drop -= 1
            raise TransportError("timeout")
        for i, (status, scripted_body, on) in enumerate(self._scripted):
            if on is None or on in path:
                del self._scripted[i]
                return FakeResponse(status, scripted_body)
        if headers.get("Authorization") != "Zoho-oauthtoken %s" % self._valid or self._valid is None:
            return FakeResponse(401, {"code": "INVALID_OAUTHTOKEN", "message": "invalid oauth token", "status": "error"})
        return self._route(method, path, body or {})

    def _token(self, params) -> FakeResponse:
        if params.get("grant_type") != "refresh_token" or not params.get("refresh_token") or not params.get("client_id"):
            return FakeResponse(200, {"error": "invalid_request"})
        self.tokens_issued += 1
        self._valid = "fake-access-%d" % self.tokens_issued
        return FakeResponse(200, {"access_token": self._valid, "expires_in": 3600, "api_domain": "https://" + self.api_host,
                                  "token_type": "Bearer"})

    def _route(self, method: str, path: str, body: dict) -> FakeResponse:
        parts = [p for p in path.split("/") if p]  # crm, v8, ...
        rest = parts[2:]
        if method != "POST":
            return FakeResponse(400, {"code": "INVALID_REQUEST_METHOD", "message": "the http request method type is not a valid one", "status": "error"})
        if rest == ["Leads", "upsert"]:
            return self._upsert(body)
        if rest == ["Tasks"]:
            return self._task(body)
        if len(rest) == 3 and rest[0] == "Leads" and rest[2] == "Notes":
            return self._note(rest[1], body)
        return FakeResponse(404, {"code": "INVALID_URL_PATTERN", "message": "Please check if the URL trying to access is a correct one", "status": "error"})

    def _upsert(self, body: dict) -> FakeResponse:
        rec = (body.get("data") or [{}])[0]
        if not rec.get("Last_Name"):
            return FakeResponse(400, {"code": "MANDATORY_NOT_FOUND", "message": "required field not found",
                                      "details": {"api_name": "Last_Name"}, "status": "error"})
        email = (rec.get("Email") or "").lower()
        for lead_id, existing in self.leads.items():
            if email and "Email" in (body.get("duplicate_check_fields") or ["Email"]) and (existing.get("Email") or "").lower() == email:
                existing.update(rec)
                data = _ok(id=lead_id)
                data.update({"duplicate_field": "Email", "action": "update", "message": "record updated"})
                return FakeResponse(200, {"data": [data]})
        lead_id = self._id()
        self.leads[lead_id] = dict(rec)
        data = _ok(id=lead_id)
        data.update({"duplicate_field": None, "action": "insert"})
        return FakeResponse(200, {"data": [data]})

    def _task(self, body: dict) -> FakeResponse:
        rec = (body.get("data") or [{}])[0]
        if not rec.get("Subject"):
            return FakeResponse(400, {"code": "MANDATORY_NOT_FOUND", "message": "required field not found",
                                      "details": {"api_name": "Subject"}, "status": "error"})
        self.tasks.append({**rec, "_lar_id": body.get("lar_id")})
        return FakeResponse(200, {"data": [_ok(id=self._id())]})

    def _note(self, lead_id: str, body: dict) -> FakeResponse:
        rec = (body.get("data") or [{}])[0]
        if lead_id not in self.leads:
            return FakeResponse(400, {"code": "INVALID_DATA", "message": "the related id given seems to be invalid", "status": "error"})
        if not rec.get("Note_Content"):
            return FakeResponse(400, {"code": "MANDATORY_NOT_FOUND", "message": "required field not found", "status": "error"})
        self.notes.append({"lead_id": lead_id, "content": rec["Note_Content"]})
        return FakeResponse(200, {"data": [_ok(id=self._id())]})
