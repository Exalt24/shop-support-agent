"""Google Calendar booking: a chat visitor picks one of the free times and the agent puts it on the business's calendar.

Written against the pages saved in docs/vendor-contract/gcal-*.txt. Never run against a live Google account: the tests use the
stand-in in agent/gcal_fake.py. Each rule below names the saved line it comes from.

  Freebusy: query (gcal-freebusy-query.txt): POST /calendar/v3/freeBusy with timeMin, timeMax (RFC3339), timeZone and items[].
    The answer holds one entry per calendar with `busy[]` ("The (inclusive) start ... The (exclusive) end") and an optional
    `errors[]` ("if computation for the calendar failed", for example notFound). A calendar with errors was NOT read, so it is never
    treated as free: the page shows these arrive inside a successful response.
  Events: insert (gcal-events-insert.txt): POST /calendars/{calendarId}/events. A caller may set the event `id`: "lowercase letters
    a-v and digits 0-9", 5 to 1024 characters, "unique per calendar"; "we cannot guarantee that ID collisions will be detected at
    event creation time". So the id is derived from the slot and the visitor (a retry carries the same id), a 409 `duplicate` is
    answered by reading the event back, and after an unknown outcome the event is looked up BEFORE it is sent again, because the
    id alone is not a guarantee. `sendUpdates` is "all", "externalOnly" or "none"; "all" is what makes Google email the guest.
  Handle API errors (gcal-handle-api-errors.txt): 401 "Get a new access token using the long-lived refresh token" (done once, through
    the token provider the caller supplies); 403 rateLimitExceeded and userRateLimitExceeded, 429 and 5xx are answered with
    "exponential backoff"; 400, other 403 and 404 are permanent, "do not retry".

The OAuth exchange itself is the caller's: the saved pages do not print the refresh request, so this module takes a token provider
(`provider(force)`; force=True means the last token was refused) and never builds that request by hand.

What goes into the event is the visitor's cleaned name, their email as the guest and the slot. The conversation steps the agent
audits hold counts and kinds only, never the name or the email.
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .zoho_crm import HttpxTransport, Transport, TransportError

API_BASE = "https://www.googleapis.com/calendar/v3"
MAX_BACKOFF = 30.0
RATE_REASONS = ("rateLimitExceeded", "userRateLimitExceeded")
SEND_UPDATES = ("all", "externalOnly", "none")
EMAIL_RE = re.compile(r"^[^@\s<>]{1,64}@[^@\s<>]{1,190}\.[^@\s<>]{2,24}$")
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z .'-]{0,58}[A-Za-z.]$")


class GoogleCalendarError(RuntimeError):
    """Google refused for a reason a retry cannot fix, or the retries ran out, or the calendar could not be read."""

    def __init__(self, status: int, reason: str):
        super().__init__(f"google calendar {status} {reason}")
        self.status, self.reason = status, reason


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(text: str) -> datetime:
    dt = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("a calendar time without an offset")
    return dt


def event_id_for(calendar_id: str, start: datetime, who: str) -> str:
    """Stable per calendar, slot and visitor, so a retry carries the same id. Hex digits are inside Google's a-v and 0-9 alphabet."""
    return hashlib.sha256(f"{calendar_id}|{_iso(start)}|{who.strip().lower()}".encode("utf-8")).hexdigest()[:40]


def clean_name(text: str) -> str | None:
    """A name as a person writes it: letters, spaces, a dot, an apostrophe or a hyphen. A phone number, an email or a sentence is not one."""
    flat = " ".join(str(text).split())
    if not NAME_RE.match(flat) or len(flat.split()) > 4:
        return None
    return flat


@dataclass(frozen=True)
class Rules:
    """When this business takes appointments, in its own time zone."""
    tz: str = "America/Chicago"
    weekdays: tuple[int, ...] = (0, 1, 2, 3, 4)      # Monday is 0
    open_hour: int = 9
    close_hour: int = 17
    slot_minutes: int = 30
    lead_minutes: int = 120
    horizon_days: int = 14
    offer: int = 3


@dataclass(frozen=True)
class Slot:
    start: datetime
    end: datetime

    def label(self) -> str:
        return "%s at %s %s" % (self.start.strftime("%a %d %b"), self.start.strftime("%I:%M %p").lstrip("0"), self.start.tzname())


@dataclass
class InsertResult:
    status: str                      # "created" or "duplicate"
    event_id: str


@dataclass
class BookResult:
    status: str                      # booked, duplicate, taken, unavailable, refused
    event_id: str | None = None
    slot: Slot | None = None
    code: str | None = None


class GoogleCalendar:
    """The Calendar REST calls this agent needs: free/busy, insert, get. The token provider is the caller's."""

    def __init__(self, calendar_id: str, token_provider: Callable[[bool], str], http: Transport | None = None, *,
                 base: str = API_BASE, sleep: Callable[[float], None] = time.sleep, max_attempts: int = 4):
        self._cal, self._tokens = calendar_id, token_provider
        self._token = token_provider(False)
        self._http = http or HttpxTransport()
        self._base, self._sleep, self._max = base.rstrip("/"), sleep, max_attempts

    @staticmethod
    def _reason(r) -> str:
        try:
            return str(r.json()["error"]["errors"][0]["reason"])
        except (ValueError, KeyError, IndexError, TypeError):
            return ""

    def _send(self, method: str, path: str, *, params: dict | None = None, body: Any = None,
              before_retry: Callable[[], Any] | None = None):
        url = f"{self._base}{path}"
        refreshed, unknown, wait = False, False, 0.5
        for attempt in range(1, self._max + 1):
            if unknown and before_retry is not None:
                early = before_retry()                  # an unknown outcome: look before sending it again
                if early is not None:
                    return early
            headers = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}
            try:
                r = self._http.request(method, url, headers=headers, params=params, body=body)
            except TransportError:
                if attempt == self._max:
                    raise GoogleCalendarError(0, "transport")
                unknown = True
                self._sleep(wait); wait *= 2
                continue
            s = r.status_code
            if s == 401 and not refreshed:
                refreshed = True
                self._token = self._tokens(True)
                continue
            reason = self._reason(r)
            if s == 429 or (s == 403 and reason in RATE_REASONS) or s >= 500:
                if attempt == self._max:
                    raise GoogleCalendarError(s, reason or "retries_exhausted")
                unknown = unknown or s >= 500
                self._sleep(min(wait, MAX_BACKOFF)); wait *= 2
                continue
            return r
        raise GoogleCalendarError(0, "exhausted")

    def busy(self, start: datetime, end: datetime, tz: str) -> list[tuple[datetime, datetime]]:
        body = {"timeMin": _iso(start), "timeMax": _iso(end), "timeZone": tz, "items": [{"id": self._cal}]}
        r = self._send("POST", "/freeBusy", body=body)
        if r.status_code != 200:
            raise GoogleCalendarError(r.status_code, self._reason(r) or "freebusy")
        entry = (r.json().get("calendars") or {}).get(self._cal)
        if entry is None or entry.get("errors"):
            raise GoogleCalendarError(200, "calendar_not_read")      # an errors[] entry means it was not read: never "free"
        try:
            return [(_parse(b["start"]), _parse(b["end"])) for b in entry.get("busy") or []]
        except (KeyError, ValueError, TypeError):
            raise GoogleCalendarError(200, "unreadable_busy_time")

    def get(self, event_id: str) -> dict | None:
        r = self._send("GET", f"/calendars/{self._cal}/events/{event_id}")
        if r.status_code == 200:
            return r.json()
        if r.status_code == 404:
            return None
        raise GoogleCalendarError(r.status_code, self._reason(r) or "get_event")

    def insert(self, event_id: str, event: dict, send_updates: str = "all") -> InsertResult:
        if send_updates not in SEND_UPDATES:
            raise ValueError("sendUpdates must be one of %s" % ", ".join(SEND_UPDATES))

        def look():
            """Run before a re-send after an unknown outcome: the event if it exists, None when it does not or the lookup failed."""
            try:
                found = self.get(event_id)
            except GoogleCalendarError:
                return None
            return _Found(found) if found else None

        r = self._send("POST", f"/calendars/{self._cal}/events", params={"sendUpdates": send_updates},
                       body={**event, "id": event_id}, before_retry=look)
        if isinstance(r, _Found):
            return InsertResult("duplicate", event_id)          # a retry found the first attempt's event
        if r.status_code == 200:
            return InsertResult("created", event_id)
        if r.status_code == 409 and self._reason(r) == "duplicate":
            same = self.get(event_id)
            if same and _same_start(same, event):
                return InsertResult("duplicate", event_id)
            raise GoogleCalendarError(409, "id_collision")
        raise GoogleCalendarError(r.status_code, self._reason(r) or "insert")


class _Found:
    """A looked-up event standing in for the insert answer."""

    def __init__(self, event: dict):
        self.event = event


def _same_start(existing: dict, wanted: dict) -> bool:
    try:
        return _parse(existing["start"]["dateTime"]) == _parse(wanted["start"]["dateTime"])
    except (KeyError, ValueError, TypeError):
        return False


def _overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end      # [start, end): a slot that begins when a busy block ends is free


class BookingDesk:
    """The business rules over a calendar: which times to offer, and whether a chosen time may still be booked."""

    def __init__(self, calendar: GoogleCalendar, rules: Rules | None = None, *, send_updates: str = "all",
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.calendar, self.rules, self.send_updates, self._now = calendar, rules or Rules(), send_updates, now

    def _tz(self) -> ZoneInfo:
        return ZoneInfo(self.rules.tz)

    def _day_slots(self, d: date):
        step = timedelta(minutes=self.rules.slot_minutes)
        t = datetime.combine(d, dtime(self.rules.open_hour), self._tz())
        close = datetime.combine(d, dtime(self.rules.close_hour), self._tz())
        while t + step <= close:
            yield Slot(t, t + step)
            t += step

    def offers(self) -> list[Slot]:
        """The next free times, at most one a day, from the calendar's own busy answer. Raises when the calendar could not be read."""
        rules, tz, now = self.rules, self._tz(), self._now()
        earliest = now + timedelta(minutes=rules.lead_minutes)
        first_day = earliest.astimezone(tz).date()
        last_day = first_day + timedelta(days=rules.horizon_days)
        window_end = datetime.combine(last_day + timedelta(days=1), dtime(0), tz)
        busy = self.calendar.busy(earliest, window_end, rules.tz)
        out: list[Slot] = []
        d = first_day
        while d <= last_day and len(out) < rules.offer:
            if d.weekday() in rules.weekdays:
                for s in self._day_slots(d):
                    if s.start >= earliest and not any(_overlaps(s.start, s.end, b0, b1) for b0, b1 in busy):
                        out.append(s)
                        break          # one a day, so the three choices are three different days
            d += timedelta(days=1)
        return out

    def rules_ok(self, slot: Slot, now: datetime) -> bool:
        """A chosen time is bookable only if it is one of this business's own slots and far enough ahead."""
        try:
            local = slot.start.astimezone(self._tz())
        except (ValueError, TypeError, AttributeError):
            return False
        if slot.end - slot.start != timedelta(minutes=self.rules.slot_minutes):
            return False
        if local.weekday() not in self.rules.weekdays or slot.start < now + timedelta(minutes=self.rules.lead_minutes):
            return False
        return any(s.start == slot.start for s in self._day_slots(local.date()))

    def book(self, slot: Slot, name: str, email: str | None) -> BookResult:
        now = self._now()
        if not self.rules_ok(slot, now):
            return BookResult("unavailable", slot=slot)
        who = clean_name(name)
        if who is None:
            return BookResult("refused", slot=slot, code="bad_name")
        try:
            busy = self.calendar.busy(slot.start, slot.end, self.rules.tz)     # read again right before the write
        except GoogleCalendarError as e:
            return BookResult("refused", slot=slot, code=e.reason)
        if any(_overlaps(slot.start, slot.end, b0, b1) for b0, b1 in busy):
            return BookResult("taken", slot=slot)
        guest = email if email and EMAIL_RE.match(email) else None
        event: dict[str, Any] = {
            "summary": "Appointment: %s" % who,
            "description": "Booked through the support chat." + (" Contact: %s" % guest if guest else ""),
            "start": {"dateTime": slot.start.isoformat(), "timeZone": self.rules.tz},
            "end": {"dateTime": slot.end.isoformat(), "timeZone": self.rules.tz},
        }
        if guest:
            event["attendees"] = [{"email": guest}]
        eid = event_id_for(self.calendar._cal, slot.start, guest or who)
        try:
            res = self.calendar.insert(eid, event, self.send_updates if guest else "none")
        except GoogleCalendarError as e:
            return BookResult("refused", slot=slot, code=e.reason)
        return BookResult("booked" if res.status == "created" else "duplicate", eid, slot)


@dataclass
class Turn:
    reply: str
    escalate: str | None = None


@dataclass
class _State:
    step: str                          # "offered" or "need_name"
    slots: list[Slot]
    chosen: Slot | None = None
    unclear: int = 0
    at: datetime | None = None


BOOK_RE = re.compile(r"\b(appointments?|booking|book\s+(?:a|an|me|us|the|in|my|our)\b|reserve\s+(?:a|an|my)\s+(?:slot|time|spot))", re.I)
CANCEL_RE = re.compile(r"\b(cancel|never\s*mind|forget\s+it|stop)\b", re.I)
ORDINALS = {"first": 1, "second": 2, "third": 3}
TTL = timedelta(minutes=30)


class BookingChat:
    """The conversation: offer three times, take a pick, take a name, book. Every reply is written here in code from the calendar's
    own times, so no model wording and no grounding guard is involved and a time that was never offered can never be said."""

    def __init__(self, desk: BookingDesk):
        self.desk, self.states = desk, {}

    def _offer(self, key: str, steps: list[str]) -> Turn:
        try:
            slots = self.desk.offers()
        except GoogleCalendarError as e:
            self.states.pop(key, None)
            steps.append("booking: calendar not read (%s)" % e.reason)
            return Turn("I could not read the calendar just now, so a teammate will follow up with you.", "calendar unavailable")
        if not slots:
            self.states.pop(key, None)
            steps.append("booking: no free time offered")
            return Turn("I could not find a free time in the next two weeks, so a teammate will follow up with you.", "no free booking time")
        self.states[key] = _State("offered", slots, at=self.desk._now())
        steps.append("booking: offered %d time(s)" % len(slots))
        listing = ", ".join("%d) %s" % (i, s.label()) for i, s in enumerate(slots, 1))
        return Turn("These times are free: %s. Reply with the number you want, or say cancel." % listing)

    @staticmethod
    def _pick(message: str, count: int) -> int | None:
        low = message.lower()
        found = {int(m) for m in re.findall(r"\b([1-9])\b", low)} | {n for w, n in ORDINALS.items() if re.search(r"\b%s\b" % w, low)}
        found = {n for n in found if 1 <= n <= count}
        return found.pop() if len(found) == 1 else None

    def converse(self, key: str, message: str, email: str | None, steps: list[str]) -> Turn | None:
        now = self.desk._now()
        st = self.states.get(key)
        if st and st.at and now - st.at > TTL:
            self.states.pop(key, None)
            steps.append("booking: an old conversation was dropped")
            st = None
        if st is None:
            return self._offer(key, steps) if BOOK_RE.search(message) else None
        if CANCEL_RE.search(message):
            self.states.pop(key, None)
            steps.append("booking: cancelled by the visitor")
            return Turn("No problem, I have not booked anything.")
        if st.step == "offered":
            n = self._pick(message, len(st.slots))
            if n is None:
                st.unclear += 1
                if st.unclear >= 2:
                    self.states.pop(key, None)
                    steps.append("booking: choice not understood twice")
                    return Turn("I could not tell which time you want, so a teammate will follow up with you.", "booking choice unclear")
                return Turn("Please reply with 1, 2 or 3 for the time you want, or say cancel.")
            st.chosen, st.step, st.unclear, st.at = st.slots[n - 1], "need_name", 0, now
            steps.append("booking: a time was chosen")
            return Turn("What name should I put the booking under?")
        who = clean_name(message)
        if who is None:
            st.unclear += 1
            if st.unclear >= 2:
                self.states.pop(key, None)
                steps.append("booking: name not understood twice")
                return Turn("I could not read a name, so a teammate will follow up with you.", "booking name unclear")
            return Turn("Please send just your name, for example Maria Santos.")
        assert st.chosen is not None
        res = self.desk.book(st.chosen, who, email)
        if res.status in ("booked", "duplicate"):
            self.states.pop(key, None)
            steps.append("booking: %s" % res.status)
            invite = " An invitation is on its way to your email." if email and EMAIL_RE.match(email) and self.desk.send_updates != "none" else ""
            return Turn("You are booked for %s under the name %s.%s" % (st.chosen.label(), who, invite))
        if res.status in ("taken", "unavailable"):
            steps.append("booking: the chosen time was no longer free")
            again = self._offer(key, steps)
            return Turn("That time is no longer free. " + again.reply, again.escalate)
        self.states.pop(key, None)
        steps.append("booking: refused (%s)" % res.code)
        return Turn("I could not complete the booking, so a teammate will follow up with you.", "booking failed")
