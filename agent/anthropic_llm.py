"""A client for the Anthropic Messages API, behind the same two-line LLM interface the Groq client has.

Never run against the live API (no key or credit was used); tests use tests/anthropic_standin.py, a server written from the pages
saved in docs/vendor-contract/anthropic/ (retrieved 2026-11-06). Each rule below names the page it comes from. Anything those
pages do not say is left out, and the README lists it under "Not done".

  Endpoint and headers (api-messages-create.txt, api-overview-authentication-headers.txt): "POST /v1/messages" at
    https://api.anthropic.com; "Authorization: Bearer <token>, where <token> is your API key" (required "unless x-api-key is set";
    x-api-key is "Legacy fallback for Authorization, still supported"), "anthropic-version" (required, "2026-10-21"),
    "content-type: application/json", and "anthropic-workspace-id" ("Required with an API key that isn't scoped to a workspace").
  Body (api-messages-create.txt): "max_tokens", "messages" and "model" are required; "system" is a string or an array of text blocks.
  No sampling parameters (api-versioning.txt, 2026-10-21): "The temperature, top_p, and top_k request parameters are removed. A
    request that sends any of them returns a 400 error on every model." So there is no temperature here, unlike the Groq client.
  Thinking (api-errors.txt, models-overview.txt): Claude Sonnet 5.5 thinks adaptively when the field is omitted. "Use thinking:
    {"type": "between_tools"} for the lowest thinking setting"; it "turns off up-front thinking" and is accepted by Sonnet 5.5
    only ("to any model other than Claude Sonnet 5.5 returns a 400"). A support reply is short and needs no up-front thinking, so
    that is sent for claude-sonnet-5-5 and for no other model. The saved pages do not say whether thinking tokens count against
    max_tokens, so a max_tokens stop with no text is a named error either way (LLMTruncatedError, has_text False).
  Answer (api-messages-create.txt, handling-stop-reasons.txt): "content" is a list of blocks; the reply is the "text" blocks.
    stop_reason is end_turn, max_tokens, stop_sequence, tool_use, pause_turn, refusal or model_context_window_exceeded; a
    refusal "is a normal HTTP 200 response, not an error" and carries stop_details {type, category, explanation}.
  Errors (api-errors.txt): "a top-level error object that always includes a type and message", plus a request_id. 400, 401, 402,
    403, 404, 413 are the caller's to fix; 429 rate_limit_error; 500 api_error; 504 timeout_error; 529 overloaded_error. "The
    official SDK automatically retries transient failures (connection errors, rate limits, and 5xx server errors) with exponential
    backoff, twice by default, honoring the retry-after header when present." This client does the same.
  Rate limits (api-rate-limits.txt): "When the response has a retry-after header, wait that many seconds before retrying." A
    spend-cap 429 "has no retry-after header", carries error.details.error_code "enforced_spend_limit_reached" and "Retrying ...
    fails until access resumes", so it is never retried.

What a failure shows: a short message with the status, the vendor's error type and a truncated vendor message. The key is never
in a message (it is also masked if the vendor's text happens to echo it), and a body that is not the documented error object is
not echoed at all. The messages end up in Result.steps, the audit log and the console, so they stay short on purpose.
"""
from __future__ import annotations

import math
import re
import threading
import time
from typing import Callable, Mapping

import httpx

from .config import ANTHROPIC_MODEL, ANTHROPIC_URL, ANTHROPIC_VERSION
from .llm import LLMError

SONNET_5_5 = "claude-sonnet-5-5"
BETWEEN_TOOLS = {"type": "between_tools"}
MAX_RETRIES = 2  # "twice by default"
BACKOFF_BASE_SECONDS = 1.0  # exponential: 1 s, 2 s. The pages give no base, so this is this repo's choice.
MAX_RETRY_AFTER_SECONDS = 30.0  # a shopper is waiting: a longer retry-after than this fails fast instead. This repo's choice.
ERROR_TEXT_CHARS = 200
KEY_OK = re.compile(r"^[\x21-\x7e]{8,}$")  # printable ASCII, no space: keeps a CR or LF out of a header
WORKSPACE_OK = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
SPEND_LIMIT_CODE = "enforced_spend_limit_reached"
REQUEST_ID_OK = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


class AnthropicError(LLMError):
    """Base for everything this client raises, so a caller can tell this vendor's failures from another provider's."""


class LLMConfigError(AnthropicError):
    """The key, workspace id or max_tokens is unusable. Raised before any request is made."""


class LLMTransportError(AnthropicError):
    """No HTTP answer: connection refused, dropped, timed out."""


class LLMAPIError(AnthropicError):
    """A non-200 answer. `error_type` is the vendor's (rate_limit_error, overloaded_error ...), `retry_after` the header in seconds."""

    def __init__(self, message: str, *, status: int, error_type: str = "", request_id: str = "", retry_after: float | None = None):
        super().__init__(message)
        self.status, self.error_type, self.request_id, self.retry_after = status, error_type, request_id, retry_after

    @property
    def retryable(self) -> bool:
        return self.status == 429 or self.status >= 500


class LLMSpendLimitError(LLMAPIError):
    """The 429 that means the monthly spend cap is reached: retrying cannot help until access resumes."""

    @property
    def retryable(self) -> bool:
        return False


class LLMRefusalError(AnthropicError):
    """stop_reason refusal, delivered as HTTP 200. `category` is stop_details.category."""

    def __init__(self, message: str, *, category: str = ""):
        super().__init__(message)
        self.category = category


class LLMTruncatedError(AnthropicError):
    """stop_reason max_tokens (or model_context_window_exceeded): the reply is cut off, or there is none."""

    def __init__(self, message: str, *, has_text: bool):
        super().__init__(message)
        self.has_text = has_text


class LLMEmptyError(AnthropicError):
    """The model finished (end_turn) and wrote no text."""


class LLMBadResponseError(AnthropicError):
    """HTTP 200 whose body is not the documented message, or whose stop_reason this client did not ask for."""


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


def _tidy(text: object, key: str = "", limit: int = ERROR_TEXT_CHARS) -> str:
    s = " ".join(str(text).split())
    if key:
        s = s.replace(key, "[key]")
    return s[:limit]


def parse_retry_after(value: str | None) -> float | None:
    """The header is "the number of seconds to wait". Anything else (absent, negative, nan, a date) is treated as absent."""
    if value is None:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


class AnthropicLLM:
    def __init__(self, key: str, model: str = ANTHROPIC_MODEL, *, workspace: str = "", url: str = ANTHROPIC_URL,
                 thinking: Mapping[str, str] | None = None, timeout: float = 60.0, max_retries: int = MAX_RETRIES,
                 sleep: Callable[[float], None] | None = None):
        if not KEY_OK.match(key or ""):
            raise LLMConfigError("ANTHROPIC_API_KEY is missing or malformed")
        if workspace and not WORKSPACE_OK.match(workspace):
            raise LLMConfigError("ANTHROPIC_WORKSPACE_ID is malformed")
        self._key, self._model, self._workspace, self._url = key, model, workspace, url
        # An explicit setting wins; otherwise the lowest thinking setting for Sonnet 5.5 and nothing for any other model.
        self._thinking = dict(thinking) if thinking is not None else (dict(BETWEEN_TOOLS) if model == SONNET_5_5 else None)
        self._timeout, self._max_retries = timeout, max_retries
        self._sleep = sleep if sleep is not None else (lambda seconds: time.sleep(seconds))

    def complete(self, system: str, user: str, *, max_tokens: int = 1800) -> str:
        if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens < 1:
            raise LLMConfigError("max_tokens must be a positive integer")
        body: dict = {"model": self._model, "max_tokens": max_tokens, "system": system,
                      "messages": [{"role": "user", "content": user}]}
        if self._thinking:
            body["thinking"] = self._thinking
        attempt = 0
        while True:
            try:
                r = self._post(body)
            except httpx.HTTPError as e:
                err = LLMTransportError("transport error: %s" % type(e).__name__)
                # a read timeout means the model was already working for the whole timeout: not repeated
                if attempt < self._max_retries and not isinstance(e, httpx.ReadTimeout):
                    self._sleep(BACKOFF_BASE_SECONDS * 2 ** attempt)
                    attempt += 1
                    continue
                raise err from e
            if r.status_code == 200:
                return self._reply(r)
            err = self._api_error(r)
            if not err.retryable or attempt >= self._max_retries:
                raise err
            wait = BACKOFF_BASE_SECONDS * 2 ** attempt
            if err.retry_after is not None:
                if err.retry_after > MAX_RETRY_AFTER_SECONDS:
                    raise err  # the vendor asks for longer than a waiting shopper gets
                wait = max(wait, err.retry_after)
            self._sleep(wait)
            attempt += 1

    def _post(self, body: dict) -> httpx.Response:
        headers = {"Authorization": "Bearer " + self._key, "anthropic-version": ANTHROPIC_VERSION}
        if self._workspace:
            headers["anthropic-workspace-id"] = self._workspace
        return _client().post(self._url, headers=headers, json=body, timeout=self._timeout)

    def _api_error(self, r: httpx.Response) -> LLMAPIError:
        request_id = r.headers.get("request-id", "")
        request_id = request_id if REQUEST_ID_OK.match(request_id) else ""
        retry_after = parse_retry_after(r.headers.get("retry-after"))
        error_type, message, code = "", "", ""
        try:
            data = r.json()
        except ValueError:
            data = None
        if isinstance(data, dict) and isinstance(data.get("error"), dict):
            e = data["error"]
            error_type = _tidy(e.get("type") or "", self._key, 60)
            message = _tidy(e.get("message") or "", self._key)
            details = e.get("details")
            code = details.get("error_code") if isinstance(details, dict) else ""
        text = "HTTP %d" % r.status_code
        if error_type:
            text += " " + error_type
        text += ": " + (message or "no error object in the response")
        if request_id:
            text += " (request-id %s)" % request_id
        kind = LLMSpendLimitError if r.status_code == 429 and code == SPEND_LIMIT_CODE else LLMAPIError
        return kind(text, status=r.status_code, error_type=error_type, request_id=request_id, retry_after=retry_after)

    def _reply(self, r: httpx.Response) -> str:
        try:
            data = r.json()
        except ValueError:
            raise LLMBadResponseError("HTTP 200 but the body is not JSON") from None
        if not isinstance(data, dict) or data.get("type") != "message" or not isinstance(data.get("content"), list):
            raise LLMBadResponseError("HTTP 200 but the body is not a message object")
        stop = data.get("stop_reason")
        if stop == "refusal":
            details = data.get("stop_details")
            category = _tidy(details.get("category") or "", self._key, 40) if isinstance(details, dict) else ""
            raise LLMRefusalError("refused by the model, category=%s" % (category or "unknown"), category=category)
        text = "".join(b["text"] for b in data["content"]
                       if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)).strip()
        if stop in ("max_tokens", "model_context_window_exceeded"):
            if text:
                raise LLMTruncatedError("reply cut off, stop_reason=%s" % stop, has_text=True)
            raise LLMTruncatedError("no text before the limit, stop_reason=%s" % stop, has_text=False)
        if stop != "end_turn":
            raise LLMBadResponseError("unexpected stop_reason=%s" % _tidy(stop, self._key, 40))
        if not text:
            raise LLMEmptyError("empty completion, stop_reason=end_turn")
        return text
