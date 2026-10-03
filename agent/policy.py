"""Policy retrieval over a markdown file: split on headings, score by shared words.

Deliberately small. The point of the demo is the grounding around it, not a vector store: for a
handful of policy sections, word overlap finds the right one, and every section keeps its heading
so an answer can say which policy it came from.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

STOP = frozenset("a an the and or of to in on is are was were be it its my i we you your can do does how what when where "
                 "will with for at by if this that from as not me have has".split())


@dataclass(frozen=True)
class PolicySection:
    heading: str
    text: str

    def as_text(self) -> str:
        return "Policy - %s: %s" % (self.heading, self.text)


def _stem(w: str) -> str:
    # "return", "returns" and "returned" must meet, or a question saying "return" scores zero on a
    # heading saying "Returns" and the shipping section outranks it (the first test caught exactly that).
    for suffix in ("ing", "ed", "es", "s"):
        if w.endswith(suffix) and len(w) - len(suffix) >= 4:
            return w[: -len(suffix)]
    return w


def _words(s: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"[a-z0-9]+", s.lower()) if w not in STOP and len(w) > 2}


def load_policy(path: Path) -> list[PolicySection]:
    return parse_policy(path.read_text(encoding="utf-8"))


def parse_policy(text: str) -> list[PolicySection]:
    """Sections from markdown text: each '## Heading' starts one. Used for the file and for a store's stored policy."""
    sections: list[PolicySection] = []
    heading, buf = None, []
    for line in text.splitlines():
        if line.startswith("## "):
            if heading:
                sections.append(PolicySection(heading, " ".join(buf).strip()))
            heading, buf = line[3:].strip(), []
        elif heading and line.strip():
            buf.append(line.strip())
    if heading:
        sections.append(PolicySection(heading, " ".join(buf).strip()))
    return sections


def retrieve(question: str, sections: list[PolicySection], k: int = 2) -> list[PolicySection]:
    q = _words(question)
    scored = []
    for s in sections:
        # heading words count double: "returns" in the question should land on the Returns section
        score = len(q & _words(s.text)) + 2 * len(q & _words(s.heading))
        if score:
            scored.append((score, s))
    scored.sort(key=lambda t: -t[0])
    return [s for _, s in scored[:k]]
