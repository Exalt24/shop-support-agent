"""The HTTP layer shared by the xAI chat client (xai_llm.py) and the Grok Imagine client (xai_imagine.py).

Never run against the live xAI API (no key exists, and image and video generation are paid per request); tests use
tests/xai_standin.py, a server written from the pages saved in docs/vendor-contract/xai-*.txt (retrieved 2026-11-15). Each rule below
names where it comes from; docs/xai-grok.md lists what had to be chosen because no saved page says it.

  Base URL and auth (every code sample, xai-chat-completions-guide.txt, xai-debugging-errors.txt): https://api.x.ai/v1 and the header
    "Authorization: Bearer <XAI_API_KEY>"; a 401 is "No authorization header or an invalid authorization token was provided".
  Statuses (xai-debugging-errors.txt): 400, 401, 403, 404, 405, 415, 422 with the causes in STATUS_MEANINGS below; 429 "You are sending
    requests too frequently and have reached the rate limit"; 202 is a pending deferred result, not an error.
  Backoff (xai-rate-limits.txt): "Implement exponential backoff", the sample waits 2 ** attempt seconds.
  A 503 is named in xai-api-reference-videos.txt ("Engine overload encountered mid-generation surfaces here as service_unavailable
    (HTTP 503)"), so it is retried like a 429.

What no saved page gives: the JSON body of an HTTP error, a Retry-After header, any rate limit header. So an error body is never read
and never shown (it could echo the key, and its shape is unknown); a failure shows the status and the documented cause only.
"""
from __future__ import annotations

import re
import threading
import time
from typing import Callable
from urllib.parse import urlparse

import httpx

from .config import XAI_DEFAULT_URL
from .llm import LLMError

MAX_RETRIES = 2  # a shopper is waiting, so far fewer than the sample's five. This repo's choice.
BACKOFF_BASE_SECONDS = 1.0  # the sample waits 2 ** attempt seconds: 1 s, 2 s
KEY_OK = re.compile(r"^[\x21-\x7e]{8,}\Z")  # printable ASCII, no space
MODEL_OK = re.compile(r"^[A-Za-z0-9_.\-]{1,100}\Z")
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
RETRYABLE_STATUSES = (429, 503)
STATUS_MEANINGS = {
    400: "Bad Request: the request body or URL is wrong",
    401: "Unauthorized: no API key, or an invalid one",
    403: "Forbidden: this key has no permission for that",
    404: "Not Found: the endpoint URL or the request is wrong",
    405: "Method Not Allowed: the endpoint does not take that method",
    415: "Unsupported Media Type",
    422: "Unprocessable Entity: a field in the body has an invalid format",
    429: "Too Many Requests: the rate limit was reached",
    503: "Service Unavailable: the engine is overloaded",
}


class XaiError(LLMError):
    """Base for everything the xAI clients raise. It is an LLMError, so the agent hands a failed model call to a person as it does for
    every other provider; the Grok Imagine client shares the base so one except clause covers both."""


class XaiConfigError(XaiError):
    """The key, model, URL or an argument is unusable. Raised before any request is made."""


class XaiTransportError(XaiError):
    """No HTTP answer: connection refused, dropped, timed out."""


class XaiAPIError(XaiError):
    """A status the call did not accept. `status` is the HTTP status; the message never holds the response body."""

    def __init__(self, message: str, *, status: int):
        super().__init__(message)
        self.status = status

    @property
    def retryable(self) -> bool:
        return self.status in RETRYABLE_STATUSES


class XaiBadResponseError(XaiError):
    """An accepted status whose body is not the documented answer."""


_client_lock = threading.Lock()
_shared_client: httpx.Client | None = None


def _client() -> httpx.Client:
    """One client for the process: building one loads the TLS certificates (about 140 ms) and the console builds an agent per message.
    httpx clients are safe to share between threads; the timeout is set per request."""
    global _shared_client
    with _client_lock:
        if _shared_client is None:
            _shared_client = httpx.Client()
        return _shared_client


def checked_url(url: str, name: str) -> str:
    """https, or plain http to this machine only: a bearer key must not cross the network in the clear."""
    parts = urlparse(url or "")
    if parts.scheme == "https" and parts.hostname:
        return url.rstrip("/")
    if parts.scheme == "http" and parts.hostname in LOOPBACK_HOSTS:
        return url.rstrip("/")
    raise XaiConfigError("%s must be an https URL" % name)


def checked_key(key: str) -> str:
    if not KEY_OK.match(key or ""):
        raise XaiConfigError("XAI_API_KEY is missing or malformed")
    return key


def checked_model(model: str, name: str = "the model name") -> str:
    if not MODEL_OK.match(model or ""):
        raise XaiConfigError("%s is malformed" % name)
    return model


def json_object(r: httpx.Response, what: str) -> dict:
    try:
        data = r.json()
    except ValueError:
        raise XaiBadResponseError("HTTP %d but the %s body is not JSON" % (r.status_code, what)) from None
    if not isinstance(data, dict):
        raise XaiBadResponseError("HTTP %d but the %s body is not a JSON object" % (r.status_code, what))
    return data


def is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value and abs(value) != float("inf")


class XaiHTTP:
    def __init__(self, key: str, base_url: str = XAI_DEFAULT_URL, *, timeout: float = 60.0, max_retries: int = MAX_RETRIES,
                 sleep: Callable[[float], None] | None = None):
        self._key = checked_key(key)
        self._url = checked_url(base_url, "XAI_BASE_URL")
        self._timeout, self._max_retries = timeout, max_retries
        self._sleep = sleep if sleep is not None else (lambda seconds: time.sleep(seconds))

    def request(self, method: str, path: str, body: dict | None = None, *, accept: tuple[int, ...] = (200,),
                retry_transport: bool = True) -> httpx.Response:
        """One call. A status in `accept` is returned; 429 and 503 are retried with exponential backoff; anything else raises.
        `retry_transport` is False for a paid POST: a dropped connection may be one the server already processed and billed."""
        attempt = 0
        while True:
            try:
                r = _client().request(method, self._url + path, json=body,
                                      headers={"Authorization": "Bearer " + self._key, "Accept": "application/json"},
                                      timeout=self._timeout)
            except httpx.HTTPError as e:
                # a read timeout means the server was already working for the whole timeout: not repeated
                if retry_transport and attempt < self._max_retries and not isinstance(e, httpx.ReadTimeout):
                    self._sleep(BACKOFF_BASE_SECONDS * 2 ** attempt)
                    attempt += 1
                    continue
                raise XaiTransportError("transport error: %s" % type(e).__name__) from e
            if r.status_code in accept:
                return r
            err = self._error(r.status_code)
            if not err.retryable or attempt >= self._max_retries:
                raise err
            self._sleep(BACKOFF_BASE_SECONDS * 2 ** attempt)
            attempt += 1

    @staticmethod
    def _error(status: int) -> XaiAPIError:
        meaning = STATUS_MEANINGS.get(status)
        if meaning is None:
            meaning = "an answer the saved pages do not list" if status < 500 else "a server error"
        return XaiAPIError("HTTP %d %s" % (status, meaning), status=status)
