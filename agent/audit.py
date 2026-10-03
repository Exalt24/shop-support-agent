"""Append-only audit log: what came in, what was looked up, what went out, and why.

One JSON line per conversation turn. Emails are masked so the log itself is not a second copy of
customer data; the order name and every decision are kept in full because those are what an
audit or a debugging session needs.
"""
from __future__ import annotations

import json
import time
from pathlib import Path


def mask_email(email: str) -> str:
    if "@" not in email:
        return "***"
    local, domain = email.split("@", 1)
    return (local[:1] + "***@" + domain) if local else "***@" + domain


class AuditLog:
    def __init__(self, path: Path):
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, record: dict) -> None:
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **record}
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
