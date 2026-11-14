"""A FAKE Google Calendar, in process, for the tests. No network, no Google account, no Google anywhere.

It follows the shapes on the pages saved in docs/vendor-contract/gcal-*.txt: the Bearer header and a 401 authError for a refused
token, freeBusy answering one entry per requested calendar (a calendar it does not know comes back as a 200 with `errors` and an
empty `busy`, the case the page describes), `busy` as start-inclusive end-exclusive RFC3339 times, events.insert with a caller
chosen id that must be lowercase a-v and 0-9 and 5 to 1024 characters, a 409 `duplicate` for a repeated id, the usage-limit 403 and
429 shapes and a 500 backendError. It is NOT Google: it knows no organizers, invitations, permissions or quotas, and the day a
live calendar answers differently, this file was wrong.

`detect_collisions=False` turns the 409 off, because the saved insert page says collisions "cannot be guaranteed" to be detected at
creation time; it is how the tests prove the lookup before a re-send is a guard on its own.

Faults are scripted: `script(status, body)` makes the next call answer that way (`match="/events"` restricts it to the next call
whose "METHOD url" contains that text), `drop()` fails the next call in transport before Google sees it, and `lose_response()`
lets Google create the event and then loses the answer on the way back.

It also counts two things a test must see: `duplicate_inserts`, an insert that reused an id when collisions are not detected,
and `overlap_inserts`, an event created over a time another event or an outside block already held.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from .zoho_crm import TransportError
from .zoho_fake import FakeResponse

ID_RE = re.compile(r"^[a-v0-9]{5,1024}$")


def error_body(code: int, domain: str, reason: str, message: str) -> dict:
    return {"error": {"errors": [{"domain": domain, "reason": reason, "message": message}], "code": code, "message": message}}


def _parse(text: str) -> datetime:
    return datetime.fromisoformat(str(text).replace("Z", "+00:00"))


def _z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeGoogleCalendar:
    def __init__(self, calendar: str = "biz@example.com", token: str = "tok-1", *, detect_collisions: bool = True):
        self.calendar, self.valid_token, self.detect = calendar, token, detect_collisions
        self.events: dict[str, dict] = {}
        self.external_busy: list[tuple[datetime, datetime]] = []
        self.calls: list[tuple[str, str, Any, Any]] = []
        self.refreshes = 0
        self.duplicate_inserts = 0
        self.overlap_inserts = 0
        self._scripted: list[tuple[int, Any, str | None]] = []
        self._drop = 0
        self._lose = 0
        self._lose_status: int | None = None

    # -- faults and tokens -------------------------------------------------------------------------------------------
    def script(self, status: int, body: Any = None, *, match: str | None = None) -> None:
        self._scripted.append((status, body, match))

    def drop(self, times: int = 1) -> None:
        self._drop += times

    def lose_response(self, times: int = 1, *, status: int | None = None) -> None:
        """Google creates the event and the answer never arrives (a transport error), or arrives as `status` (say a 500)."""
        self._lose += times
        self._lose_status = status

    def provider(self, first: str | None = None):
        """A token provider: returns `first` (default: the valid token) at the start and the valid token when forced."""
        state = {"first": first}

        def provide(force: bool = False) -> str:
            if force:
                self.refreshes += 1
                return self.valid_token
            return state["first"] if state["first"] is not None else self.valid_token
        return provide

    # -- the wire ----------------------------------------------------------------------------------------------------
    def request(self, method, url, *, headers, params=None, body=None):
        self.calls.append((method, url, params, body))
        if self._drop:
            self._drop -= 1
            raise TransportError("ConnectionReset")
        for i, (status, payload, match) in enumerate(self._scripted):
            if match is None or match in f"{method} {url}":
                self._scripted.pop(i)
                return FakeResponse(status, payload)
        if headers.get("Authorization") != f"Bearer {self.valid_token}":
            return FakeResponse(401, error_body(401, "global", "authError", "Invalid Credentials"))
        path = url.split("/calendar/v3", 1)[-1]
        if method == "POST" and path == "/freeBusy":
            return self._free_busy(body or {})
        prefix = f"/calendars/{self.calendar}/events"
        if method == "POST" and path == prefix:
            return self._insert(params or {}, body or {})
        if method == "GET" and path.startswith(prefix + "/"):
            found = self.events.get(path[len(prefix) + 1:])
            return FakeResponse(200, found) if found else FakeResponse(404, error_body(404, "global", "notFound", "Not Found"))
        return FakeResponse(404, error_body(404, "global", "notFound", "Not Found"))

    def _intervals(self, exclude: str | None = None) -> list[tuple[datetime, datetime]]:
        out = list(self.external_busy)
        for key, e in self.events.items():
            if key == exclude:
                continue
            out.append((_parse(e["start"]["dateTime"]), _parse(e["end"]["dateTime"])))
        return out

    def _free_busy(self, body: dict) -> FakeResponse:
        try:
            lo, hi = _parse(body["timeMin"]), _parse(body["timeMax"])
        except (KeyError, ValueError):
            return FakeResponse(400, error_body(400, "global", "required", "timeMin and timeMax are required"))
        if not lo < hi:
            return FakeResponse(400, error_body(400, "calendar", "timeRangeEmpty", "The specified time range is empty."))
        items = body.get("items") or []
        if not items:
            return FakeResponse(400, error_body(400, "global", "required", "items is required"))
        calendars: dict[str, dict] = {}
        for item in items:
            cid = item.get("id")
            if cid != self.calendar:
                calendars[cid] = {"errors": [{"domain": "global", "reason": "notFound"}], "busy": []}
                continue
            busy = [{"start": _z(a), "end": _z(b)} for a, b in sorted(self._intervals()) if a < hi and lo < b]
            calendars[cid] = {"busy": busy}
        return FakeResponse(200, {"kind": "calendar#freeBusy", "timeMin": body["timeMin"], "timeMax": body["timeMax"], "calendars": calendars})

    def _insert(self, params: dict, body: dict) -> FakeResponse:
        eid = body.get("id")
        if eid is not None and not ID_RE.match(str(eid)):
            return FakeResponse(400, error_body(400, "global", "invalid", "Invalid resource id value."))
        try:
            lo, hi = _parse(body["start"]["dateTime"]), _parse(body["end"]["dateTime"])
        except (KeyError, ValueError, TypeError):
            return FakeResponse(400, error_body(400, "global", "required", "start and end are required"))
        if not lo < hi:
            return FakeResponse(400, error_body(400, "global", "timeRangeEmpty", "The specified time range is empty."))
        if params.get("sendUpdates") not in (None, "all", "externalOnly", "none"):
            return FakeResponse(400, error_body(400, "global", "invalid", "Invalid sendUpdates value."))
        eid = str(eid or "gen%d" % (len(self.events) + 1000))
        if eid in self.events:
            if self.detect:
                return FakeResponse(409, error_body(409, "global", "duplicate", "The requested identifier already exists."))
            self.duplicate_inserts += 1
        if any(a < hi and lo < b for a, b in self._intervals(exclude=eid)):
            self.overlap_inserts += 1
        stored = {**body, "id": eid, "status": "confirmed", "_sendUpdates": params.get("sendUpdates")}
        self.events[eid] = stored
        if self._lose:
            self._lose -= 1
            if self._lose_status:
                return FakeResponse(self._lose_status, error_body(self._lose_status, "global", "backendError", "Backend Error"))
            raise TransportError("ReadTimeout")
        return FakeResponse(200, stored)
