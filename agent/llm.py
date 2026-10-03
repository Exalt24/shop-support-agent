"""One small LLM client. Two failure modes are first-class errors, never an empty answer.

A reasoning model can spend its whole completion budget thinking and return empty content with
finish_reason "length". Treating that as "no reply" would send a blank to a shopper, so it raises.
"""
from __future__ import annotations

import json
import re
from typing import Protocol

import httpx

from .config import GROQ_URL, MODEL, Settings


class LLMError(RuntimeError):
    pass


class LLM(Protocol):
    def complete(self, system: str, user: str, *, max_tokens: int = 1800) -> str: ...


class GroqLLM:
    def __init__(self, settings: Settings, model: str = MODEL, timeout: float = 60.0):
        self._key = settings.groq_key
        self._model = model
        self._timeout = timeout

    def complete(self, system: str, user: str, *, max_tokens: int = 1800) -> str:
        try:
            r = httpx.post(
                GROQ_URL,
                headers={"Authorization": "Bearer " + self._key},
                json={
                    "model": self._model,
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                    "max_tokens": max_tokens,
                    "temperature": 0,
                    "reasoning_effort": "low",
                },
                timeout=self._timeout,
            )
        except httpx.HTTPError as e:
            raise LLMError("transport error: %s" % type(e).__name__) from e
        if r.status_code != 200:
            raise LLMError("HTTP %s: %s" % (r.status_code, r.text[:160]))
        choice = r.json()["choices"][0]
        text = (choice["message"].get("content") or "").strip()
        if not text:
            raise LLMError("empty completion, finish_reason=%s" % choice.get("finish_reason"))
        return text


def parse_json(text: str) -> dict:
    """Pull the first JSON object out of a model reply, or raise."""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise LLMError("no JSON object in reply")
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise LLMError("unparseable JSON: %s" % e) from e
