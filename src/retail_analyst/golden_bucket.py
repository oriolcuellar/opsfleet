"""Golden Bucket: past (question, SQL, analyst report) trios written by human analysts.

The prototype loads them from YAML and finds the most similar ones by word overlap.
Production would use embeddings in a vector store (see docs/architecture.md).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

_WORD = re.compile(r"[a-z0-9']+")
_STOPWORDS = {"the", "and", "for", "our", "what", "how", "did", "with", "are", "was", "why",
              "who", "of", "in", "to", "by", "is", "we", "a", "an", "x", "y"}


@dataclass(frozen=True)
class Trio:
    question: str
    analyst_notes: str
    sql: str
    report: str

    def as_example(self) -> str:
        return (f"Question: {self.question}\nAnalyst notes: {self.analyst_notes.strip()}\n"
                f"SQL:\n{self.sql.strip()}\nAnalyst conclusion: {self.report.strip()}")


class GoldenBucket:
    def __init__(self, directory: Path) -> None:
        self.trios = [Trio(**yaml.safe_load(p.read_text(encoding="utf-8")))
                      for p in sorted(directory.glob("*.yaml"))]

    def search(self, question: str, k: int = 2) -> list[Trio]:
        words = _words(question)
        scored = []
        for trio in self.trios:
            overlap = len(words & _words(trio.question + " " + trio.analyst_notes))
            if overlap:
                scored.append((overlap, trio))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [trio for _, trio in scored[:k]]


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOPWORDS and len(w) > 2}
