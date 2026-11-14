"""A FAKE Notion API, in process, for the tests. No network, no integration token, no workspace, no Notion anywhere.

It follows the shapes on the pages saved in docs/vendor-contract/notion-api-*.txt: the Bearer and Notion-Version headers (the
version is required), a data source with a schema, create a page with parent data_source_id and properties that must match that
schema, a query with a title `equals` filter answered with results, has_more and next_cursor, a 429 "rate_limited" with an
integer Retry-After header and the same wait in additional_data, and a 529. It is NOT Notion: it knows no permissions or
capabilities, rollups, templates or the real limits, and the day a live workspace answers differently, this file was wrong.

Faults are scripted: `script(status, body, headers)` makes the next call answer that way, `drop()` fails the next call in
transport, and `lose_response()` lets Notion apply a write and then loses the answer, the case a lookup-before-create exists for.
"""
from __future__ import annotations

from typing import Any

from .zoho_crm import TransportError
from .zoho_fake import FakeResponse

SCHEMA = {"Ticket": "title", "Queue": "select", "Reason": "rich_text", "Intent": "rich_text", "Status": "select"}


class HeaderResponse(FakeResponse):
    def __init__(self, status_code: int, body: Any, headers: dict | None = None):
        super().__init__(status_code, body)
        self.headers = headers or {}


def rate_limited(seconds: int = 3, reason: str = "public_api_request_rate_limit") -> tuple[int, dict, dict]:
    return 429, {"object": "error", "status": 429, "code": "rate_limited", "message": "rate limited",
                 "additional_data": {"rate_limit_reason": reason, "retry_after": seconds}}, {"Retry-After": str(seconds)}


class FakeNotion:
    def __init__(self, token: str = "tok", data_source: str = "DS1"):
        self._token, self._ds = token, data_source
        self.rows: dict[str, dict] = {}
        self.calls: list[tuple[str, str, Any]] = []
        self._scripted: list[tuple[int, Any, dict]] = []
        self._drop = 0
        self._lose = 0
        self._n = 0

    def script(self, status: int, body: Any = None, headers: dict | None = None) -> None:
        self._scripted.append((status, body, headers or {}))

    def drop(self, times: int = 1) -> None:
        self._drop += times

    def lose_response(self, times: int = 1) -> None:
        self._lose += times

    def status_of(self, ticket_id: str) -> str | None:
        for row in self.rows.values():
            if row["Ticket"] == ticket_id:
                return row["Status"]
        return None

    def count(self, ticket_id: str) -> int:
        return sum(1 for r in self.rows.values() if r["Ticket"] == ticket_id)

    def request(self, method, url, *, headers, params=None, body=None):
        self.calls.append((method, url, body))
        if self._drop:
            self._drop -= 1
            raise TransportError("ConnectionReset")
        if self._scripted:
            status, payload, hdrs = self._scripted.pop(0)
            return HeaderResponse(status, payload, hdrs)
        if headers.get("Authorization") != f"Bearer {self._token}":
            return HeaderResponse(401, {"object": "error", "code": "unauthorized"})
        if not headers.get("Notion-Version"):
            return HeaderResponse(400, {"object": "error", "code": "missing_version"})
        path = url.split("/v1", 1)[1]
        b = body or {}
        if method == "POST" and path == f"/data_sources/{self._ds}/query":
            f = (b.get("filter") or {}).get("title", {}).get("equals")
            hits = [{"object": "page", "id": pid} for pid, r in self.rows.items() if f is None or r["Ticket"] == f]
            return HeaderResponse(200, {"object": "list", "results": hits[: b.get("page_size", 100)], "has_more": False, "next_cursor": None})
        if method == "POST" and path == "/pages":
            if (b.get("parent") or {}).get("data_source_id") != self._ds:
                return HeaderResponse(404, {"object": "error", "code": "object_not_found"})
            row = self._row(b.get("properties") or {})
            if isinstance(row, str):
                return HeaderResponse(400, {"object": "error", "code": "validation_error", "message": row})
            self._n += 1
            pid = f"page-{self._n}"
            self.rows[pid] = row
            if self._lose:
                self._lose -= 1
                raise TransportError("ReadTimeout")
            return HeaderResponse(200, {"object": "page", "id": pid})
        if method == "PATCH" and path.startswith("/pages/"):
            pid = path[len("/pages/"):]
            if pid not in self.rows:
                return HeaderResponse(404, {"object": "error", "code": "object_not_found"})
            sel = ((b.get("properties") or {}).get("Status") or {}).get("select", {}).get("name")
            if sel not in ("Open", "Taken", "Resolved"):
                return HeaderResponse(400, {"object": "error", "code": "validation_error"})
            self.rows[pid]["Status"] = sel
            if self._lose:
                self._lose -= 1
                raise TransportError("ReadTimeout")
            return HeaderResponse(200, {"object": "page", "id": pid})
        return HeaderResponse(404, {"object": "error", "code": "invalid_request_url"})

    @staticmethod
    def _row(props: dict):
        """Check the properties against the schema and flatten them. A string answer is the validation message."""
        if set(props) - set(SCHEMA):
            return "property not in the data source schema"
        out = {}
        for name, kind in SCHEMA.items():
            if name not in props:
                if name == "Ticket":
                    return "Ticket (title) is required"
                continue
            v = props[name]
            if kind == "title" and not (isinstance(v.get("title"), list) and v["title"]):
                return "title must be a non-empty rich text array"
            if kind == "rich_text" and not isinstance(v.get("rich_text"), list):
                return "rich_text must be an array"
            if kind == "select" and not (isinstance(v.get("select"), dict) and v["select"].get("name")):
                return "select needs a name"
            if kind == "title":
                out[name] = v["title"][0]["text"]["content"]
            elif kind == "rich_text":
                out[name] = "".join(p["text"]["content"] for p in v["rich_text"])
                if len(out[name]) > 2000:
                    return "rich_text content too long"
            else:
                out[name] = v["select"]["name"]
        return out
