"""The grounding guard: nothing reaches a shopper that the facts did not contain.

Two checks, both in code, because a prompt that says "do not invent numbers" is a request and
this is a refusal:
  1. every figure in the reply (order numbers, prices, quantities, tracking numbers, day counts)
     must also appear in the facts the model was given;
  2. no commitment the agent has no authority to make: refunds, discounts, free replacements.
     Those are a person's decision, so the reply is refused and the case goes to a human.

A digit that sits inside a word ("USB-C", "A2") is not a quantity, so the figure pattern ignores a
digit preceded by a letter. Without that the guard fires on product names and gets switched off.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Not preceded by a letter, so product codes are not read as quantities. A trailing ".0" or ".00"
# is normalised so 447.0 in the facts and 447 in the reply are the same number.
FIGURE_RE = re.compile(r"(?<![A-Za-z])\d[\d,]*(?:\.\d+)?")

PROMISE_RE = re.compile(
    r"\b(i(?:'ve| have| will| 'll)?\s*(?:approved|processed|issued|refunded)|"
    r"your refund (?:is|has been) (?:approved|processed|issued)|"
    r"(?:i can|i will|i'll) (?:give|offer|apply) you|"
    r"\d+\s*%\s*off|free replacement|full refund|discount code|"
    # outcomes the facts never state: money moving after a cancellation, "we'll proceed", and claiming a
    # notification was already sent (the first live run said tracking details "were emailed", which
    # nothing in the facts supported and which was untrue on that store)
    r"(?:payment|money|amount) will be (?:returned|refunded|credited)|will be (?:refunded|credited)|"
    r"we(?:'ll| will) (?:proceed|process|cancel it|refund)|"
    r"(?:was|were|has been|have been) (?:e-?mailed|sent to you))\b", re.I)


def _norm(fig: str) -> str:
    f = fig.replace(",", "")
    if "." in f:
        f = f.rstrip("0").rstrip(".")
    return f


WORD_NUMS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8",
             "nine": "9", "ten": "10", "eleven": "11", "twelve": "12", "fourteen": "14", "twenty": "20",
             "thirty": "30", "sixty": "60", "ninety": "90"}
_NW = "|".join(WORD_NUMS)
# A number written as a word only counts as a figure when a unit follows it ("two business days",
# "three to seven business days"), so "one of our teammates" is not read as the quantity 1. A digit-only
# check lets "five days" through when the facts say 3 to 7.
WORD_FIGURE_RE = re.compile(
    r"\b(?:%s)\b(?:\s+(?:to|or|-)\s+(?:%s)\b)?\s+(?:(?:business|working|calendar)\s+)?"
    r"(?:days?|weeks?|hours?|months?|items?|units?|percent|dollars?|usd)\b" % (_NW, _NW), re.I)


def figures(text: str) -> set[str]:
    out = {_norm(m.group(0)) for m in FIGURE_RE.finditer(text)}
    for m in WORD_FIGURE_RE.finditer(text):
        out |= {WORD_NUMS[w.lower()] for w in re.findall(r"\b(?:%s)\b" % _NW, m.group(0), re.I)}
    return out


@dataclass(frozen=True)
class GuardResult:
    ok: bool
    violations: tuple[str, ...]


def verify_grounded(reply: str, facts_text: str, *, allowed_extra: frozenset[str] = frozenset()) -> GuardResult:
    problems: list[str] = []
    allowed = figures(facts_text) | set(allowed_extra)
    for fig in sorted(figures(reply) - allowed):
        problems.append("figure not in the facts: %s" % fig)
    # A model often writes typographic apostrophes ("we’ll"); the patterns are written with a straight one, so a
    # curly apostrophe let "we’ll process the cancellation and refund" through (found 2026-10-12 reading the demo transcript).
    m = PROMISE_RE.search(reply.replace("’", "'").replace("‘", "'").replace("ʼ", "'"))
    if m:
        problems.append("commitment outside the agent's authority: %r" % m.group(0))
    return GuardResult(ok=not problems, violations=tuple(problems))
