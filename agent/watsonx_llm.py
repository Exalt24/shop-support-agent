"""A client for the IBM watsonx.ai chat API on IBM Cloud, behind the same two-line LLM interface the Groq and Claude clients have.

Never run against a live IBM Cloud account (we hold none); tests use tests/watsonx_standin.py, a server written from the pages saved
in docs/vendor-contract/ibm-watsonx/ (retrieved 2026-11-06). Each rule below names where it comes from; docs/watsonx.md lists what
had to be assumed because no saved page says it.

  Token (credentials-api-key-iam-token.txt, authenticating-for-programmatic-access.txt): "POST https://iam.cloud.ibm.com/identity/token",
    "Content-Type: application/x-www-form-urlencoded", body "grant_type=urn:ibm:params:oauth:grant-type:apikey&apikey=<key>"; "Retrieve
    the IAM bearer token from the access_token field"; "You are responsible for refreshing the token when it expires"; tokens "are valid
    for 60 minutes". The answer also carries expires_in and expiration (epoch seconds): that is from IBM's IAM page as quoted by a
    search result, because cloud.ibm.com would not load in a browser here (see docs/watsonx.md).
  Chat (chat-api.txt): "POST https://<region>.<cloud-provider-domain>/ml/v1/text/chat?version=2025-10-25" with "Authorization: Bearer",
    a body of model_id, project_id, messages (role in lower case: "The role parameter ... is case sensitive. Make sure to set the role in
    lower case") and max_completion_tokens (the apidocs call max_tokens deprecated); the answer is choices[0].message.content with a
    finish_reason and a usage object of completion_tokens, prompt_tokens, total_tokens. This agent sends no tools, so none are built.
  Errors (java-sdk-error-handling.txt, node-sdk-api-error*.txt): a body of {"errors": [{"code", "message", "more_info"}], "trace"}.
    401 authentication_token_expired: "refreshes the token and retries (once by default)". 429 token_quota_reached is the quota being
    used up; the SDKs retry 429, 502, 503, 504 and 520 with exponential backoff (java-sdk-retry-settings.txt, python-sdk-rate-limits.txt).
    finish_reason can be length, time_limit, cancelled or error besides stop.

What a failure shows: a short message with the status, the vendor's error code and a truncated vendor message, plus the trace id. The
key and the bearer token are never in a message (they are masked if the vendor's text echoes them), and a body that is not the
documented error object is not echoed at all. The messages end up in Result.steps, the audit log and the console.
"""
from __future__ import annotations

import re
import threading
import time
from typing import Callable
from urllib.parse import urlparse

import httpx

from .anthropic_llm import parse_retry_after  # a pure function: "the number of seconds to wait", anything else is absent
from .config import WATSONX_API_VERSION, WATSONX_DEFAULT_MODEL_ID, WATSONX_DEFAULT_URL, WATSONX_IAM_URL
from .llm import LLMError

MAX_RETRIES = 2  # a shopper is waiting; the IBM SDKs retry up to 10 times, which is too long for a chat reply. This repo's choice.
BACKOFF_BASE_SECONDS = 1.0  # exponential: 1 s, 2 s. No saved page gives a base for a plain HTTP client, so this is this repo's choice.
MAX_RETRY_AFTER_SECONDS = 30.0  # a longer Retry-After than this fails fast instead of keeping a shopper waiting
REFRESH_MARGIN_SECONDS = 120.0  # a token is renewed this long before it runs out, so it cannot expire between the check and the call
DEFAULT_TOKEN_SECONDS = 3600.0  # "valid for 60 minutes", used only when the IAM answer carries no expiry at all
ERROR_TEXT_CHARS = 200
KEY_OK = re.compile(r"^[\x21-\x7e]{8,}$")  # printable ASCII, no space
PROJECT_OK = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
MODEL_OK = re.compile(r"^[A-Za-z0-9_.\-/]{1,100}$")
TRACE_OK = re.compile(r"^[A-Za-z0-9_.:\-]{1,80}$")
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
QUOTA_CODE = "token_quota_reached"
STOP_REASONS_CUT_OFF = ("length", "time_limit")


class WatsonxError(LLMError):
    """Base for everything this client raises, so a caller can tell this vendor's failures from another provider's."""


class WatsonxConfigError(WatsonxError):
    """The key, project id, model id, URL or max_tokens is unusable. Raised before any request is made."""


class WatsonxTransportError(WatsonxError):
    """No HTTP answer: connection refused, dropped, timed out."""


class WatsonxAPIError(WatsonxError):
    """A non-200 answer. `code` is the vendor's error code (token_quota_reached ...), `trace` IBM's trace id."""

    def __init__(self, message: str, *, status: int, code: str = "", trace: str = "", retry_after: float | None = None):
        super().__init__(message)
        self.status, self.code, self.trace, self.retry_after = status, code, trace, retry_after

    @property
    def retryable(self) -> bool:
        return self.status == 429 or self.status >= 500


class WatsonxQuotaError(WatsonxAPIError):
    """The 429 that means the token quota is used up: waiting a few seconds cannot help."""

    @property
    def retryable(self) -> bool:
        return False


class WatsonxAuthError(WatsonxAPIError):
    """IAM refused to give a token for this key (wrong, revoked, deleted)."""


class WatsonxTruncatedError(WatsonxError):
    """finish_reason length or time_limit: the reply is cut off, or there is none."""

    def __init__(self, message: str, *, has_text: bool):
        super().__init__(message)
        self.has_text = has_text


class WatsonxEmptyError(WatsonxError):
    """The model finished (stop) and wrote no text."""


class WatsonxBadResponseError(WatsonxError):
    """HTTP 200 whose body is not the documented chat answer, or whose finish_reason this client did not ask for."""


_client_lock = threading.Lock()
_shared_client: httpx.Client | None = None


def _client() -> httpx.Client:
    """One client for the process: building one loads the TLS certificates (about 140 ms), and the console builds an agent per
    message. httpx clients are safe to share between threads; the timeout is set per request."""
    global _shared_client
    with _client_lock:
        if _shared_client is None:
            _shared_client = httpx.Client()
        return _shared_client


def _tidy(text: object, secrets: tuple[str, ...] = (), limit: int = ERROR_TEXT_CHARS) -> str:
    s = " ".join(str(text).split())
    for secret in secrets:
        if secret:
            s = s.replace(secret, "[secret]")
    return s[:limit]


def _checked_url(url: str, name: str) -> str:
    """https, or plain http to this machine only: a bearer token or a key must not cross the network in the clear."""
    parts = urlparse(url or "")
    if parts.scheme == "https" and parts.hostname:
        return url.rstrip("/")
    if parts.scheme == "http" and parts.hostname in LOOPBACK_HOSTS:
        return url.rstrip("/")
    raise WatsonxConfigError("%s must be an https URL" % name)


def _error_fields(r: httpx.Response, secrets: tuple[str, ...]) -> tuple[str, str, str]:
    """(code, message, trace) from IBM's error object {"errors": [{"code", "message"}], "trace"}, or the IAM shape
    {"errorCode", "errorMessage"}. Empty strings when the body is anything else."""
    try:
        data = r.json()
    except ValueError:
        return "", "", ""
    if not isinstance(data, dict):
        return "", "", ""
    trace = data.get("trace") if isinstance(data.get("trace"), str) else ""
    errors = data.get("errors")
    first = errors[0] if isinstance(errors, list) and errors and isinstance(errors[0], dict) else {}
    code = first.get("code") or data.get("errorCode") or ""
    message = first.get("message") or data.get("errorMessage") or ""
    # a trace id is shown only if it looks like one and is not a secret that was echoed into the field
    trace = trace if TRACE_OK.match(trace) and not any(x and x in trace for x in secrets) else ""
    return _tidy(code, secrets, 60), _tidy(message, secrets), trace


class IamTokens:
    """The IAM bearer token for one API key: fetched on first use, reused until shortly before it expires, refetched after that or
    when told the vendor refused it. The token lives in this object's memory only; it is never logged or written anywhere."""

    def __init__(self, key: str, *, url: str = WATSONX_IAM_URL, timeout: float = 30.0, max_retries: int = MAX_RETRIES,
                 sleep: Callable[[float], None] | None = None, clock: Callable[[], float] | None = None):
        self._key, self._url, self._timeout, self._max_retries = key, url, timeout, max_retries
        self._sleep = sleep if sleep is not None else (lambda seconds: time.sleep(seconds))
        self._clock = clock if clock is not None else time.time
        self._lock = threading.Lock()
        self._token = ""
        self._expires_at = 0.0
        self.fetches = 0

    @property
    def current(self) -> str:
        """The token held now, for masking in messages. Empty before the first fetch."""
        return self._token

    def get(self) -> str:
        with self._lock:
            if not self._token or self._clock() >= self._expires_at - REFRESH_MARGIN_SECONDS:
                self._fetch()
            return self._token

    def invalidate(self, refused: str) -> None:
        """The vendor refused `refused`. Dropped only if it is still the held token, so two threads that both got a 401 for the
        same old token do not throw away the fresh one the first of them fetched."""
        with self._lock:
            if self._token == refused:
                self._token = ""

    def _fetch(self) -> None:
        attempt = 0
        while True:
            try:
                r = _client().post(self._url, data={"grant_type": "urn:ibm:params:oauth:grant-type:apikey", "apikey": self._key},
                                   headers={"Accept": "application/json"}, timeout=self._timeout)
            except httpx.HTTPError as e:
                if attempt < self._max_retries and not isinstance(e, httpx.ReadTimeout):
                    self._sleep(BACKOFF_BASE_SECONDS * 2 ** attempt)
                    attempt += 1
                    continue
                raise WatsonxTransportError("IAM token request failed: transport error %s" % type(e).__name__) from e
            if r.status_code == 200:
                self._remember(r)
                return
            err = self._error(r)
            if not err.retryable or attempt >= self._max_retries:
                raise err
            self._sleep(BACKOFF_BASE_SECONDS * 2 ** attempt)
            attempt += 1

    def _error(self, r: httpx.Response) -> WatsonxAuthError:
        code, message, trace = _error_fields(r, (self._key,))
        text = "IAM token request refused: HTTP %d" % r.status_code
        if code:
            text += " " + code
        text += ": " + (message or "no error object in the response")
        return WatsonxAuthError(text, status=r.status_code, code=code, trace=trace, retry_after=parse_retry_after(r.headers.get("retry-after")))

    def _remember(self, r: httpx.Response) -> None:
        try:
            data = r.json()
        except ValueError:
            raise WatsonxBadResponseError("IAM answered 200 but the body is not JSON") from None
        token = data.get("access_token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token.strip():
            raise WatsonxBadResponseError("IAM answered 200 but the body has no access_token")
        now = self._clock()
        expires_in, expiration = data.get("expires_in"), data.get("expiration")
        if _is_number(expires_in) and expires_in > 0:
            expires_at = now + expires_in  # relative: immune to a clock that disagrees with IBM's
        elif _is_number(expiration) and expiration > 0:
            expires_at = float(expiration)
        else:
            expires_at = now + DEFAULT_TOKEN_SECONDS
        self._token, self._expires_at = token.strip(), expires_at
        self.fetches += 1


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value and abs(value) != float("inf")


class WatsonxLLM:
    def __init__(self, key: str, project_id: str, model_id: str = WATSONX_DEFAULT_MODEL_ID, *, url: str = WATSONX_DEFAULT_URL,
                 iam_url: str = WATSONX_IAM_URL, timeout: float = 60.0, max_retries: int = MAX_RETRIES,
                 sleep: Callable[[float], None] | None = None, clock: Callable[[], float] | None = None,
                 on_usage: Callable[[dict], None] | None = None):
        if not KEY_OK.match(key or ""):
            raise WatsonxConfigError("WATSONX_API_KEY is missing or malformed")
        if not PROJECT_OK.match(project_id or ""):
            raise WatsonxConfigError("WATSONX_PROJECT_ID is missing or malformed")
        if not MODEL_OK.match(model_id or ""):
            raise WatsonxConfigError("WATSONX_MODEL_ID is malformed")
        self._key, self._project, self._model = key, project_id, model_id
        self._url = _checked_url(url, "WATSONX_URL")
        self._timeout, self._max_retries, self._on_usage = timeout, max_retries, on_usage
        self._sleep = sleep if sleep is not None else (lambda seconds: time.sleep(seconds))
        self._tokens = IamTokens(key, url=_checked_url(iam_url, "the IAM URL"), sleep=self._sleep, clock=clock, max_retries=max_retries)

    def complete(self, system: str, user: str, *, max_tokens: int = 1800) -> str:
        if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens < 1:
            raise WatsonxConfigError("max_tokens must be a positive integer")
        body = {"model_id": self._model, "project_id": self._project,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "max_completion_tokens": max_tokens, "temperature": 0}
        refreshed = False
        while True:
            token = self._tokens.get()
            try:
                return self._chat(body, token)
            except WatsonxAPIError as e:
                # "refreshes the token and retries (once by default)": a second 401 means the token is not the problem
                if e.status == 401 and not refreshed:
                    refreshed = True
                    self._tokens.invalidate(token)
                    continue
                raise

    def _chat(self, body: dict, token: str) -> str:
        secrets = (self._key, token)
        attempt = 0
        while True:
            try:
                r = _client().post(self._url + "/ml/v1/text/chat", params={"version": WATSONX_API_VERSION}, json=body,
                                   headers={"Authorization": "Bearer " + token, "Accept": "application/json"}, timeout=self._timeout)
            except httpx.HTTPError as e:
                err: Exception = WatsonxTransportError("transport error: %s" % type(e).__name__)
                # a read timeout means the model was already working for the whole timeout: not repeated
                if attempt < self._max_retries and not isinstance(e, httpx.ReadTimeout):
                    self._sleep(BACKOFF_BASE_SECONDS * 2 ** attempt)
                    attempt += 1
                    continue
                raise err from e
            if r.status_code == 200:
                return self._reply(r, secrets)
            api_err = self._api_error(r, secrets)
            if not api_err.retryable or attempt >= self._max_retries:
                raise api_err
            wait = BACKOFF_BASE_SECONDS * 2 ** attempt
            if api_err.retry_after is not None:
                if api_err.retry_after > MAX_RETRY_AFTER_SECONDS:
                    raise api_err  # the vendor asks for longer than a waiting shopper gets
                wait = max(wait, api_err.retry_after)
            self._sleep(wait)
            attempt += 1

    def _api_error(self, r: httpx.Response, secrets: tuple[str, ...]) -> WatsonxAPIError:
        code, message, trace = _error_fields(r, secrets)
        text = "HTTP %d" % r.status_code
        if code:
            text += " " + code
        text += ": " + (message or "no error object in the response")
        if trace:
            text += " (trace %s)" % trace
        kind = WatsonxQuotaError if r.status_code == 429 and code == QUOTA_CODE else WatsonxAPIError
        return kind(text, status=r.status_code, code=code, trace=trace, retry_after=parse_retry_after(r.headers.get("retry-after")))

    def _reply(self, r: httpx.Response, secrets: tuple[str, ...]) -> str:
        try:
            data = r.json()
        except ValueError:
            raise WatsonxBadResponseError("HTTP 200 but the body is not JSON") from None
        choices = data.get("choices") if isinstance(data, dict) else None
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise WatsonxBadResponseError("HTTP 200 but the body has no choices (blocked by moderation, or not a chat answer)")
        usage = data.get("usage")
        if self._on_usage is not None and isinstance(usage, dict):
            self._on_usage({k: usage[k] for k in ("prompt_tokens", "completion_tokens", "total_tokens") if _is_number(usage.get(k))})
        choice = choices[0]
        message = choice.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        text = content.strip() if isinstance(content, str) else ""
        finish = choice.get("finish_reason")
        finish = finish.lower() if isinstance(finish, str) else None
        if finish in STOP_REASONS_CUT_OFF:
            if text:
                raise WatsonxTruncatedError("reply cut off, finish_reason=%s" % finish, has_text=True)
            raise WatsonxTruncatedError("no text before the limit, finish_reason=%s" % finish, has_text=False)
        if finish != "stop":
            raise WatsonxBadResponseError("unexpected finish_reason=%s" % _tidy(finish, secrets, 40))
        if not text:
            raise WatsonxEmptyError("empty completion, finish_reason=stop")
        return text
