"""A client for the xAI chat completions API (Grok), behind the same two-line LLM interface the Groq, Claude and watsonx clients have.

Never run against the live xAI API (no key exists); tests use tests/xai_standin.py, a server written from the pages saved in
docs/vendor-contract/xai-*.txt (retrieved 2026-11-15). docs/xai-grok.md lists what had to be chosen because no saved page says it.

  Request (xai-api-reference-chat-completions.txt, xai-chat-completions-guide.txt): "POST https://api.x.ai/v1/chat/completions" with
    "Authorization: Bearer", a body of model, messages (role system then user) and max_completion_tokens ("max_tokens" is marked
    DEPRECATED in the reference), temperature 0 (the reference allows 0 to 2). No tools, no stream, no stop: "presencePenalty,
    frequencyPenalty, and stop cannot be used with reasoning models" (xai-reasoning.txt).
  Reasoning (xai-reasoning.txt): grok-4.7, grok-4.6 and grok-4.5 "support the reasoning_effort parameter", default "high", "Reasoning
    cannot be disabled". A support turn is latency-sensitive, so this client sends "low" unless told otherwise, and only to those
    three models (the page names no others).
  Answer: choices[0].message.content, message.refusal ("the reason given by model if the model is unable to generate a response"),
    finish_reason "stop" or "length", and usage with prompt_tokens, completion_tokens, total_tokens and cost_in_usd_ticks.
    max_completion_tokens "only applies to visible output tokens (i.e. does not apply to tokens used for reasoning)".

Statuses, backoff and what is never shown of a failure: see xai_http.py. A reply that is cut off, empty, refused, or finished for any
reason other than "stop" is a named error, never a blank sent to a shopper.
"""
from __future__ import annotations

import re
from typing import Callable

from .config import XAI_DEFAULT_MODEL, XAI_DEFAULT_URL
from .xai_http import (MAX_RETRIES, XaiBadResponseError, XaiConfigError, XaiError, XaiHTTP, checked_model, is_number, json_object)

REASONING_EFFORTS = ("low", "medium", "high", "xhigh")
REASONING_EFFORT_MODELS = ("grok-4.7", "grok-4.6", "grok-4.5")
FINISH_OK = re.compile(r"^[A-Za-z0-9_-]{1,40}\Z")
USAGE_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens", "cost_in_usd_ticks")


class XaiTruncatedError(XaiError):
    """finish_reason length: the reply is cut off, or there is none."""

    def __init__(self, message: str, *, has_text: bool):
        super().__init__(message)
        self.has_text = has_text


class XaiEmptyError(XaiError):
    """The model finished (stop) and wrote no text."""


class XaiRefusalError(XaiError):
    """The answer carries a refusal: the model declined to reply."""


class XaiLLM:
    def __init__(self, key: str, model: str = XAI_DEFAULT_MODEL, *, base_url: str = XAI_DEFAULT_URL, reasoning_effort: str = "low",
                 timeout: float = 60.0, max_retries: int = MAX_RETRIES, sleep: Callable[[float], None] | None = None,
                 on_usage: Callable[[dict], None] | None = None):
        self._model = checked_model(model, "XAI_MODEL")
        if reasoning_effort not in REASONING_EFFORTS:
            raise XaiConfigError("reasoning_effort must be one of: " + ", ".join(REASONING_EFFORTS))
        self._effort, self._on_usage = reasoning_effort, on_usage
        self._http = XaiHTTP(key, base_url, timeout=timeout, max_retries=max_retries, sleep=sleep)

    def complete(self, system: str, user: str, *, max_tokens: int = 1800) -> str:
        if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens < 1:
            raise XaiConfigError("max_tokens must be a positive integer")
        body: dict = {"model": self._model,
                      "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                      "max_completion_tokens": max_tokens, "temperature": 0}
        if self._model in REASONING_EFFORT_MODELS:
            body["reasoning_effort"] = self._effort
        r = self._http.request("POST", "/chat/completions", body)
        return self._reply(json_object(r, "chat"))

    def _reply(self, data: dict) -> str:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise XaiBadResponseError("HTTP 200 but the body has no choices")
        usage = data.get("usage")
        if self._on_usage is not None and isinstance(usage, dict):
            self._on_usage({k: usage[k] for k in USAGE_FIELDS if is_number(usage.get(k))})
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, dict):
            raise XaiBadResponseError("HTTP 200 but the choice has no message")
        refusal = message.get("refusal")
        if isinstance(refusal, str) and refusal.strip():
            raise XaiRefusalError("the model refused to reply")
        if message.get("tool_calls"):
            raise XaiBadResponseError("the model asked for a tool call, and this client sends no tools")
        content = message.get("content")
        text = content.strip() if isinstance(content, str) else ""
        finish = choice.get("finish_reason")
        if finish == "length":
            if text:
                raise XaiTruncatedError("reply cut off, finish_reason=length", has_text=True)
            raise XaiTruncatedError("no text before the limit, finish_reason=length", has_text=False)
        if finish != "stop":
            shown = finish if isinstance(finish, str) and FINISH_OK.match(finish) else "?"  # a value never seen before is not echoed raw
            raise XaiBadResponseError("unexpected finish_reason=%s" % shown)
        if not text:
            raise XaiEmptyError("empty completion, finish_reason=stop")
        return text
