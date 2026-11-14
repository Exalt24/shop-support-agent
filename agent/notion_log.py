"""Notion ticket log: every escalated turn is one row in a Notion data source, and the Take and Resolve clicks move its Status.

Written against the pages saved in docs/vendor-contract/notion-api-*.txt. Never run against a live Notion workspace: the tests use
the stand-in in agent/notion_fake.py. Each rule names its source.

  Headers (notion-api-versioning.txt): "Set the version by including a Notion-Version header. Setting this header is required."
    and "The most recent Notion-Version is 2026-03-11." Authorization is a Bearer token (notion-api-query-a-data-source.txt).
  Create a page (notion-api-create-a-page.txt): the SDK call is pages.create with parent {data_source_id} and properties
    keyed by the data source's own property names; "For a page in a data source, property names or IDs must match the parent data
    source's schema", and "Formula, rollup, ID, creation, and last-edit values are set by Notion". The saved page shows the SDK
    call and not the URL, so POST /v1/pages is the endpoint that call wraps, and a live run confirms it. A 403 means the connection
    lacks Insert Content.
  Query a data source (notion-api-query-a-data-source.txt): POST https://api.notion.com/v1/data_sources/{id}/query with a
    filter object, answered with results, has_more and next_cursor; "about 3 requests per second per connection".
  Rate limits (notion-api-request-limits.txt): a 429 "rate_limited" or a 529 "service_overload", "Read Retry-After and pause new
    requests for at least that many seconds. ... The header value is an integer number of seconds. Use the returned value rather
    than assuming a fixed wait." "public_api_request_blocked" says "Retrying won't help".
  Notion has no idempotency key, so a create is made safe by looking the ticket up first: a row whose Ticket title equals the
  ticket id is the same ticket, whether the first create answered or its answer was lost.

A row holds the ticket id, queue, reason, intent and status. Never the customer's words, email or phone.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .zoho_crm import HttpxTransport, Transport, TransportError

API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2026-03-11"
MAX_RETRY_WAIT = 120.0
RETRY_STATUSES = (429, 529)
STATES = {"open": "Open", "taken": "Taken", "resolved": "Resolved"}


def _text(value: str, limit: int) -> list[dict]:
    return [{"text": {"content": " ".join(str(value).split())[:limit]}}]


def row_properties(ticket: Mapping[str, Any]) -> dict:
    return {"Ticket": {"title": _text(ticket["id"], 100)},
            "Queue": {"select": {"name": " ".join(str(ticket.get("queue", "support")).split())[:80]}},
            "Reason": {"rich_text": _text(ticket.get("reason", "needs a person"), 300)},
            "Intent": {"rich_text": _text(ticket.get("intent", ""), 80)},
            "Status": {"select": {"name": STATES["open"]}}}


class _Found:
    """A row a recheck found, shaped like a successful create answer."""
    status_code = 200

    def __init__(self, page_id: str):
        self._id = page_id

    def json(self) -> dict:
        return {"id": self._id}


@dataclass
class LogResult:
    status: str                      # "created", "duplicate" or "refused"
    page_id: str | None = None
    code: str | None = None


class NotionTicketLog:
    def __init__(self, token: str, data_source_id: str, http: Transport | None = None, *, base: str = API_BASE,
                 sleep: Callable[[float], None] = time.sleep, max_attempts: int = 4):
        self._token, self._ds, self._base = token, data_source_id, base.rstrip("/")
        self._http = http or HttpxTransport()
        self._sleep, self._max = sleep, max_attempts
        self.pages: dict[str, str] = {}

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}", "Notion-Version": NOTION_VERSION, "Content-Type": "application/json"}

    def _call(self, method: str, path: str, body: Any = None, recheck: Callable[[], str] | None = None):
        """One request with the documented retry rule. Returns (response or None, failure code or None).

        `recheck` runs before every retry of a write: a lost answer or a 5xx does not say whether Notion applied it, so the row is
        looked up again and, when it is there, the write is not repeated."""
        wait = 0.5
        for attempt in range(1, self._max + 1):
            if attempt > 1 and recheck is not None:
                already = recheck()
                if already:
                    return _Found(already), None
            try:
                r = self._http.request(method, f"{self._base}{path}", headers=self._headers(), body=body)
            except TransportError:
                r = None
            if r is not None and r.status_code == 200:
                return r, None
            if r is not None and r.status_code in RETRY_STATUSES:
                if self._blocked(r):
                    return None, "blocked"
                if attempt < self._max:
                    self._sleep(min(self._retry_after(r), MAX_RETRY_WAIT))
                continue
            if r is not None and r.status_code < 500:
                return None, f"http_{r.status_code}"          # 400, 401, 403, 404, 409: a retry cannot fix it
            if attempt < self._max:
                self._sleep(wait)
                wait *= 2
        return None, "exhausted"

    @staticmethod
    def _retry_after(r) -> float:
        try:
            return max(0.0, float((getattr(r, "headers", None) or {}).get("Retry-After")))
        except (TypeError, ValueError):
            return 1.0

    @staticmethod
    def _blocked(r) -> bool:
        try:
            return r.json()["additional_data"]["rate_limit_reason"] == "public_api_request_blocked"
        except (KeyError, TypeError, ValueError):
            return False

    def _find(self, tid: str):
        body = {"filter": {"property": "Ticket", "title": {"equals": tid}}, "page_size": 1}
        r, code = self._call("POST", f"/data_sources/{self._ds}/query", body)
        if r is None:
            return None, code
        results = r.json().get("results") or []
        return (str(results[0]["id"]) if results else ""), None

    def record(self, ticket: Mapping[str, Any]) -> LogResult:
        tid = str(ticket["id"])
        if tid in self.pages:
            return LogResult("duplicate", self.pages[tid])
        found, code = self._find(tid)
        if found is None:
            return LogResult("refused", None, code)
        if found:
            self.pages[tid] = found
            return LogResult("duplicate", found)
        r, code = self._call("POST", "/pages", {"parent": {"data_source_id": self._ds}, "properties": row_properties(ticket)},
                             recheck=lambda: self._find(tid)[0] or "")
        if r is None:
            return LogResult("refused", None, code)
        page_id = str(r.json()["id"])
        self.pages[tid] = page_id
        return LogResult("created", page_id)

    def set_status(self, ticket_id: str, state: str) -> LogResult:
        """Move a ticket's row to Open, Taken or Resolved. A row that was never created is created first by the caller."""
        if state not in STATES:
            return LogResult("refused", None, "bad_state")
        page_id = self.pages.get(ticket_id)
        if page_id is None:
            found, code = self._find(ticket_id)
            if not found:
                return LogResult("refused", None, code or "no_row")
            page_id = self.pages[ticket_id] = found
        r, code = self._call("PATCH", f"/pages/{page_id}", {"properties": {"Status": {"select": {"name": STATES[state]}}}})
        return LogResult("updated", page_id) if r is not None else LogResult("refused", page_id, code)
