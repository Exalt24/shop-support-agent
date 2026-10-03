"""When a person takes over. Rules in code, so the model cannot talk its way past them.

The model also gets a vote (it flags anger, a dispute, a request for a human), but the keyword floor
below applies whatever it says. A legal threat or a chargeback is never an automated reply.
"""
from __future__ import annotations

import re

HARD_TRIGGERS = [
    ("legal", re.compile(r"\b(lawyer|attorney|sue|lawsuit|legal action|solicitor|court)\b", re.I)),
    ("payment dispute", re.compile(r"\b(chargeback|dispute (?:the )?charge|charged twice|double charged|fraud(?:ulent)?)\b", re.I)),
    ("asked for a person", re.compile(r"\b(speak|talk) to (?:a |an )?(?:human|person|manager|agent|someone)\b|\bhuman agent\b|\bmanager\b", re.I)),
    ("safety", re.compile(r"\b(caught fire|burn(?:t|ed)|shock(?:ed)?|injur(?:y|ed)|unsafe)\b", re.I)),
]

HOLDING_REPLY = ("Thanks for telling us. I've passed this to a member of our team, who will reply to you directly. "
                 "You don't need to do anything else for now.")


def hard_reason(message: str) -> str | None:
    for reason, pattern in HARD_TRIGGERS:
        if pattern.search(message):
            return reason
    return None


def decide(message: str, model_flags: dict) -> str | None:
    """Return the escalation reason, or None to let the agent answer."""
    reason = hard_reason(message)
    if reason:
        return reason
    for flag in ("legal_threat", "chargeback", "wants_human", "angry"):
        if model_flags.get(flag) is True:
            return "model flagged: %s" % flag
    return None
