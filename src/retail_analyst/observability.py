"""Structured trace of every turn, written as JSON lines to ``.data/traces.jsonl``.

Each user message gets a trace id. Everything that happens while answering it (LLM calls,
SQL queries, retries, fallbacks, blocked queries, confirmations, errors) is logged as an
event with that id, so a failing conversation can be replayed step by step.
"""

from __future__ import annotations

import json
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass
class Turn:
    trace_id: str
    started: float
    events: list[dict[str, Any]] = field(default_factory=list)
    outcome: str = "running"
    latency_s: float = 0.0

    def count(self, kind: str, **match: Any) -> int:
        return sum(1 for e in self.events
                   if e["kind"] == kind and all(e.get(k) == v for k, v in match.items()))

    def total(self, key: str) -> int:
        return sum(e.get(key, 0) for e in self.events)

    def summary(self) -> dict[str, Any]:
        return {
            "llm_calls": self.count("llm_call"),
            "input_tokens": self.total("input_tokens"),
            "output_tokens": self.total("output_tokens"),
            "sql_ok": self.count("sql", ok=True),
            "sql_failed": self.count("sql", ok=False),
            "sql_blocked": self.count("sql_blocked"),
            "retries": self.count("retry"),
            "fallbacks": self.count("fallback"),
        }


class Tracer:
    def __init__(self, path: Path, user_id: str, conversation_id: str) -> None:
        self._path = path
        self.user_id = user_id
        self.conversation_id = conversation_id
        self.turns: list[Turn] = []
        self.current: Turn | None = None

    def start_turn(self, question: str) -> Turn:
        self.current = Turn(trace_id=uuid.uuid4().hex[:10], started=time.perf_counter())
        self.turns.append(self.current)
        self._write("turn_start", {"question": question})
        return self.current

    def end_turn(self, outcome: str, answer: str) -> Turn:
        turn = self.current
        assert turn is not None
        turn.outcome = outcome
        turn.latency_s = round(time.perf_counter() - turn.started, 2)
        self._write("turn_end", {"outcome": outcome, "latency_s": turn.latency_s,
                                 **turn.summary(), "answer": answer})
        self.current = None
        return turn

    def event(self, kind: str, **data: Any) -> None:
        if self.current is not None:
            self.current.events.append({"kind": kind, **data})
        self._write(kind, data)

    def session_stats(self) -> dict[str, Any]:
        done = [t for t in self.turns if t.outcome != "running"]
        totals: Counter[str] = Counter()
        for turn in done:
            totals.update(turn.summary())
        latencies = sorted(t.latency_s for t in done)
        return {
            "turns": len(done),
            "outcomes": dict(Counter(t.outcome for t in done)),
            "avg_latency_s": round(sum(latencies) / len(latencies), 2) if latencies else 0,
            "max_latency_s": latencies[-1] if latencies else 0,
            **totals,
        }

    def _write(self, kind: str, data: dict[str, Any]) -> None:
        record = {"ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
                  "trace_id": self.current.trace_id if self.current else None,
                  "conversation_id": self.conversation_id, "user": self.user_id,
                  "kind": kind, **data}
        with self._path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, default=str) + "\n")
