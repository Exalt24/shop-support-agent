"""Break each guard on purpose and prove the suite goes red. A guard whose removal changes nothing is decoration.

Every mutant is applied to the real source, the suite is run, and the file is restored byte-exact
(verified, not assumed). A mutant that leaves the suite green is a missing test, and fails this run.
A mutant that HANGS the suite counts as caught too, but is reported separately because it is a
different failure mode from red.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

MUTANTS = [
    # (file, original, replacement, what removing it would let through)
    ("agent/guard.py", "for fig in sorted(figures(reply) - allowed):", "for fig in sorted(set()):",
     "an invented figure reaches the shopper"),
    ("agent/guard.py", "m = PROMISE_RE.search(reply)", "m = None",
     "a refund or discount promise reaches the shopper"),
    ("agent/guard.py", "    for m in WORD_FIGURE_RE.finditer(text):", "    for m in ():",
     "'five business days' passes when the facts say 3 to 7 (a number written as a word)"),
    ("agent/shopify.py", "!= email.strip().lower():", "== '\\x00':",
     "anyone can read any order by number (identity check removed)"),
    ("agent/shopify.py", "if body.get(\"errors\"):", "if False:",
     "a throttle delivered as HTTP 200 reads as 'no such order'"),
    ("agent/escalation.py", "lawyer|attorney|sue|lawsuit|legal action|solicitor|court", "zzzzqqqq",
     "a legal threat gets an automated reply"),
    ("agent/core.py", "        if check.ok:", "        if True:",
     "an ungrounded reply is sent instead of retried or escalated"),
    ("agent/core.py", "return self._escalate(res, \"store lookup failed\")", "return res",
     "a store outage produces a blank reply instead of a person"),
    ("agent/shopify.py", "\"status:active %s\" % text", "\"%s\" % text",
     "draft and archived products are offered to shoppers"),
    ("agent/core.py", "if order.fulfillment_status == \"unfulfilled\":", "if False:",
     "an unshipped order's customer is never told it can still be cancelled"),
    ("agent/policy.py", "for suffix in (\"ing\", \"ed\", \"es\", \"s\"):", "for suffix in ():",
     "'return' no longer meets 'Returns' and the wrong policy is retrieved"),
]


def run_suite() -> tuple[str, str]:
    try:
        p = subprocess.run([sys.executable, "-m", "pytest", "tests", "-q", "-x", "-p", "no:cacheprovider"],
                           cwd=ROOT, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return "hang", ""
    return ("green" if p.returncode == 0 else "red"), p.stdout[-600:]


def main() -> int:
    baseline, out = run_suite()
    if baseline != "green":
        print("BASELINE IS NOT GREEN, mutation results would mean nothing\n" + out)
        return 2
    survivors = []
    for rel, old, new, consequence in MUTANTS:
        path = ROOT / rel
        original = path.read_bytes()
        text = original.decode("utf-8")
        if text.count(old) != 1:
            print("FAIL  %s: anchor %r found %d times, expected exactly 1" % (rel, old, text.count(old)))
            return 2
        try:
            path.write_bytes(text.replace(old, new).encode("utf-8"))
            outcome, _ = run_suite()
        finally:
            path.write_bytes(original)
        if path.read_bytes() != original:
            print("FAIL  %s was not restored byte-exact" % rel)
            return 2
        caught = outcome in ("red", "hang")
        print("%-6s %-22s %s" % ("caught" if caught else "ALIVE", rel, consequence) + ("  [suite hung]" if outcome == "hang" else ""))
        if not caught:
            survivors.append(rel + ": " + consequence)
    if survivors:
        print("\n%d mutant(s) survived: a guard with no test behind it." % len(survivors))
        return 1
    print("\nAll %d mutants caught, every file restored byte-exact." % len(MUTANTS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
