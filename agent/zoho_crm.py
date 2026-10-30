"""Zoho CRM client: OAuth2 refresh-token flow, Lead upsert, Task and Note, with retries. Never run against a live account.

Everything here is written against the vendor pages saved in docs/vendor-contract/ (fetched 2026-10-30). Each rule below
quotes the line it comes from. Where a page did not say, the code does not guess: it sends nothing for that field.

  Refresh (zoho-crm-v8-oauth-refresh-access-token.txt):
    "{Accounts_URL}/oauth/v2/token?refresh_token={refresh_token}&client_id={client_id}&client_secret={client_secret}&grant_type=refresh_token"
    "You must use your domain-specific Zoho Accounts URL to refresh your access token."
    the answer carries access_token, "expires_in": 3600 and "api_domain": "https://www.zohoapis.com".
  Header (zoho-crm-v8-oauth-generate-tokens.txt): 'send the token ... with the value "Zoho-oauthtoken {access_token}" for each endpoint'.
  Token limits (zoho-crm-v8-oauth-token-validity.txt): "Each access token is valid for one hour." and "You can generate a maximum of
    10 access tokens from a refresh token in a span of 10 minutes." and "Reuse valid tokens to avoid this exception." So the
    token is cached until a minute before expiry and refreshed at most once per rejected call.
  Domains (zoho-crm-v8-multi-dc.txt): "AU: https://accounts.zoho.com.au". API hosts (zoho-crm-usecase-api-domains.txt):
    "www.zohoapis.com (US), www.zohoapis.eu (EU), www.zohoapis.com.au (AU), www.zohoapis.jp (JP), www.zohoapis.ca (CA)";
    CN is "https://www.zohoapis.com.cn" on the multi-dc page.
  Upsert (zoho-crm-v8-upsert-records.txt): "POST /{module_api_name}/upsert", "{api-domain}/crm/{version}/{module_api_name}/upsert",
    'Leads ... Email' is the system-defined duplicate check field, "A maximum of 100 records can be inserted/updated per API call.",
    the answer per record has "action": "insert" or "update" and details.id.
  Insert (zoho-crm-v8-insert-records.txt): Leads need '"Last_Name" - Single Line'; Tasks need '"Subject" - Multi Line'.
    "lar_id ... The unique ID of the assignment rule you want to trigger while inserting a record ... supported for ... Tasks".
  Task fields (zoho-community-kaizen-36-tasks-api.txt, written by Zoho staff against v2, and the v8 insert page sample that
    shows What_Id, "$se_module", Status, Subject): Due_Date is "Date in yyyy-mm-dd format"; Status "Not Started" is allowed;
    Priority is one of "High", "Highest", "Low", "Lowest", "Normal".
  Notes (zoho-crm-v8-create-notes.txt): "{api-domain}/crm/{version}/{module_api_name}/{record_id}/Notes", Parent_Id and
    Note_Content are mandatory.
  Errors (zoho-crm-v8-status-codes.txt): 401 "Invalid API key provided.", 429 "Number of API requests for the 24 hour period
    is exceeded or the concurrency limit of the user for the app is exceeded.", 500 "Generic error ... unexpected server
    error." Record-level: DUPLICATE_DATA and MANDATORY_NOT_FOUND arrive as HTTP 400 with a code and a message; "In case of
    partial success or failure, the API returns an HTTP status - 207".
  Credits (zoho-crm-v8-api-limits-and-credits.txt): "Insert/Update/Upsert 1 credit for every 10 records", "All other APIs 1".
    One new lead costs three credits here (upsert, task, note).

The Owner field of a Task is NOT sent: none of the saved pages names its API key for an insert. An optional assignment rule
(lar_id, which is documented) is how a queue reaches a person.

Retry rules: 408, 429, 5xx and transport errors are retried with exponential backoff (4 attempts); any other 4xx is a
failure that is raised once with the vendor's code and message. Zoho documents no idempotency header, so a 5xx that was
committed server side and then retried can in principle double a Task or a Note; the lead itself cannot double because the
upsert checks Email.
"""
from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

import httpx

API_VERSION = "v8"
# region -> (accounts URL, API domain). The accounts URLs are on the refresh and multi-dc pages, the API hosts on the multi-dc
# page (eu, cn) and the use-case page (us, au, jp, ca, in).
REGIONS: dict[str, tuple[str, str]] = {
    "us": ("https://accounts.zoho.com", "https://www.zohoapis.com"),
    "au": ("https://accounts.zoho.com.au", "https://www.zohoapis.com.au"),
    "eu": ("https://accounts.zoho.eu", "https://www.zohoapis.eu"),
    "in": ("https://accounts.zoho.in", "https://www.zohoapis.in"),
    "jp": ("https://accounts.zoho.jp", "https://www.zohoapis.jp"),
    "ca": ("https://accounts.zohocloud.ca", "https://www.zohoapis.ca"),
    "cn": ("https://accounts.zoho.com.cn", "https://www.zohoapis.com.cn"),
}
KNOWN_API_DOMAINS = frozenset(api for _, api in REGIONS.values())
RETRY_STATUSES = frozenset({408, 429})  # plus every 5xx
MAX_ATTEMPTS = 4
BACKOFF_BASE_SECONDS = 0.5
TOKEN_SKEW_SECONDS = 60  # refresh a minute before the hour is up
DEFAULT_EXPIRES_IN = 3600

_EMAIL_IN_TEXT = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")


def scrub(text: object, limit: int = 160) -> str:
    """A vendor message goes into the audit and the steps, so an email that a message happens to echo is masked first."""
    s = _EMAIL_IN_TEXT.sub("[email]", str(text))
    return " ".join(s.split())[:limit]


class ZohoError(RuntimeError):
    """A Zoho call that failed. `code` is the vendor's code when it gave one (MANDATORY_NOT_FOUND), else HTTP_<status>."""

    def __init__(self, message: str, *, status: int | None = None, code: str = "ZOHO_ERROR", retryable: bool = False):
        super().__init__(message)
        self.status, self.code, self.retryable = status, code, retryable


class TransportError(RuntimeError):
    """The request never got an answer (timeout, connection reset)."""


class HttpResponse(Protocol):
    status_code: int
    text: str

    def json(self) -> Any: ...


class Transport(Protocol):
    def request(self, method: str, url: str, *, headers: Mapping[str, str], params: Mapping[str, str] | None = None,
                body: Any = None) -> HttpResponse: ...


class HttpxTransport:
    """The real transport. Tests and the demo inject a fake instead (agent/zoho_fake.py)."""

    def __init__(self, timeout: float = 20.0):
        self._client = httpx.Client(timeout=timeout)

    def request(self, method, url, *, headers, params=None, body=None):
        try:
            return self._client.request(method, url, headers=dict(headers), params=params, json=body)
        except httpx.HTTPError as e:
            raise TransportError(type(e).__name__) from e


@dataclass(frozen=True)
class ZohoSettings:
    """From the environment only: ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN, ZOHO_REGION (us, au, eu, in, jp, ca, cn).
    An Australian account is ZOHO_REGION=au, which means accounts.zoho.com.au and www.zohoapis.com.au."""
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    refresh_token: str = field(repr=False)
    region: str = "us"

    def __post_init__(self):
        if self.region not in REGIONS:
            raise ValueError("unknown Zoho region %r, expected one of %s" % (self.region, ", ".join(REGIONS)))

    @property
    def accounts_url(self) -> str:
        return REGIONS[self.region][0]

    @property
    def api_domain(self) -> str:
        return REGIONS[self.region][1]

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ZohoSettings":
        env = os.environ if env is None else env
        names = ("ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN")
        missing = [n for n in names if not env.get(n)]
        if missing:
            raise RuntimeError("missing configuration: " + ", ".join(missing))
        return cls(env["ZOHO_CLIENT_ID"], env["ZOHO_CLIENT_SECRET"], env["ZOHO_REFRESH_TOKEN"],
                   (env.get("ZOHO_REGION") or "us").strip().lower())


@dataclass(frozen=True)
class UpsertResult:
    id: str
    action: str  # "insert" or "update", as Zoho reports it
    duplicate_field: str | None = None


class CrmClient(Protocol):
    """What lead capture needs from a CRM. ZohoCrmClient is the only real implementation."""
    def upsert_lead(self, record: dict, duplicate_check_fields: list[str]) -> UpsertResult: ...
    def create_task(self, record: dict, assignment_rule_id: str | None = None) -> str: ...
    def add_note(self, module: str, record_id: str, content: str) -> str: ...


class ZohoCrmClient:
    def __init__(self, settings: ZohoSettings, transport: Transport | None = None, *,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 max_attempts: int = MAX_ATTEMPTS):
        self._s = settings
        self._transport = transport or HttpxTransport()
        self._clock, self._sleep, self._max_attempts = clock, sleep, max_attempts
        self._api_domain = settings.api_domain
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    # -- token ------------------------------------------------------------------------------------------------------

    def _access_token(self) -> str:
        with self._lock:
            if self._token and self._clock() < self._expires_at:
                return self._token
            return self._refresh()

    def _forget(self, token: str) -> None:
        with self._lock:
            if self._token == token:  # another caller may already have replaced it
                self._token = None

    def _refresh(self) -> str:
        try:
            r = self._transport.request("POST", self._s.accounts_url + "/oauth/v2/token", headers={}, params={
                "refresh_token": self._s.refresh_token, "client_id": self._s.client_id,
                "client_secret": self._s.client_secret, "grant_type": "refresh_token"})
        except TransportError as e:
            raise ZohoError("token refresh: transport error %s" % e, code="TRANSPORT", retryable=True) from e
        try:
            body = r.json()
        except ValueError:
            body = {}
        token = body.get("access_token") if isinstance(body, dict) else None
        if r.status_code != 200 or not token:
            # Zoho answers a bad or throttled refresh with an "error" key (and the throttle with "Access Denied"): never loop on it.
            why = body.get("error") if isinstance(body, dict) else None
            raise ZohoError("token refresh refused: %s" % scrub(why or "HTTP %s" % r.status_code), status=r.status_code,
                            code="TOKEN_REFRESH_FAILED")
        try:
            lifetime = int(body.get("expires_in") or DEFAULT_EXPIRES_IN)
        except (TypeError, ValueError):
            lifetime = DEFAULT_EXPIRES_IN
        self._token, self._expires_at = token, self._clock() + max(lifetime - TOKEN_SKEW_SECONDS, 0)
        host = str(body.get("api_domain") or "").rstrip("/")
        if host in KNOWN_API_DOMAINS:  # the token answer names the data centre; an unknown host is ignored, never trusted
            self._api_domain = host
        return token

    # -- one call, with the retry rules -----------------------------------------------------------------------------

    def _delay(self, failures: int) -> float:
        return BACKOFF_BASE_SECONDS * (2 ** (failures - 1))

    def _send(self, method: str, path: str, body: Any) -> dict:
        refreshed, failures = False, 0
        while True:
            token = self._access_token()
            try:
                r = self._transport.request(method, "%s/crm/%s%s" % (self._api_domain, API_VERSION, path), headers={
                    "Authorization": "Zoho-oauthtoken " + token, "Content-Type": "application/json"}, body=body)
            except TransportError as e:
                failures += 1
                if failures >= self._max_attempts:
                    raise ZohoError("transport error %s after %d attempts" % (e, failures), code="TRANSPORT", retryable=True) from e
                self._sleep(self._delay(failures))
                continue
            status = r.status_code
            if 200 <= status < 300:
                return self._first_record(r)
            if status == 401 and not refreshed:
                refreshed = True  # one refresh and one retry; a second 401 is a real failure
                self._forget(token)
                continue
            err = self._error(r)
            if status in RETRY_STATUSES or 500 <= status < 600:
                failures += 1
                if failures >= self._max_attempts:
                    err.retryable = True
                    raise err
                self._sleep(self._delay(failures))
                continue
            raise err

    @staticmethod
    def _body(r: HttpResponse) -> Any:
        try:
            return r.json()
        except ValueError:
            return None

    def _error(self, r: HttpResponse) -> ZohoError:
        body = self._body(r)
        item = body if isinstance(body, dict) else {}
        if isinstance(item.get("data"), list) and item["data"] and isinstance(item["data"][0], dict):
            item = item["data"][0]
        code = str(item.get("code") or "HTTP_%d" % r.status_code)
        return ZohoError("%s: %s" % (code, scrub(item.get("message") or r.text or "no message")), status=r.status_code, code=code)

    def _first_record(self, r: HttpResponse) -> dict:
        body = self._body(r)
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            raise ZohoError("unreadable answer from Zoho", status=r.status_code, code="BAD_RESPONSE")
        first = data[0]
        if first.get("status") != "success" or first.get("code") != "SUCCESS":  # HTTP 207 carries record-level errors
            code = str(first.get("code") or "RECORD_ERROR")
            raise ZohoError("%s: %s" % (code, scrub(first.get("message") or "no message")), status=r.status_code, code=code)
        return first

    # -- the three calls lead capture needs -------------------------------------------------------------------------

    def upsert_lead(self, record: dict, duplicate_check_fields: list[str]) -> UpsertResult:
        payload: dict = {"data": [record]}
        if duplicate_check_fields:
            payload["duplicate_check_fields"] = list(duplicate_check_fields)
        first = self._send("POST", "/Leads/upsert", payload)
        rec_id = (first.get("details") or {}).get("id")
        if not rec_id:
            raise ZohoError("upsert answered without a record id", code="BAD_RESPONSE")
        return UpsertResult(id=str(rec_id), action=str(first.get("action") or "insert"), duplicate_field=first.get("duplicate_field"))

    def create_task(self, record: dict, assignment_rule_id: str | None = None) -> str:
        payload: dict = {"data": [record]}
        if assignment_rule_id:
            payload["lar_id"] = assignment_rule_id  # "must be provided parallel to the key data"
        first = self._send("POST", "/Tasks", payload)
        return str((first.get("details") or {}).get("id") or "")

    def add_note(self, module: str, record_id: str, content: str) -> str:
        payload = {"data": [{"Parent_Id": {"module": {"api_name": module}, "id": record_id}, "Note_Content": content}]}
        first = self._send("POST", "/%s/%s/Notes" % (module, record_id), payload)
        return str((first.get("details") or {}).get("id") or "")
