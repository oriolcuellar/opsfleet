"""Saved Reports library (SQLite). Every operation is restricted to the report owner."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id              TEXT PRIMARY KEY,
    owner           TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    title           TEXT NOT NULL,
    body            TEXT NOT NULL,
    created_at      TEXT NOT NULL
)
"""


@dataclass(frozen=True)
class Report:
    id: str
    owner: str
    conversation_id: str
    title: str
    body: str
    created_at: str


class ReportStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        with self._connect() as con:
            con.execute(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self._path)
        con.row_factory = sqlite3.Row
        try:
            with con:  # commits on success, rolls back on error
                yield con
        finally:
            con.close()

    def save(self, owner: str, conversation_id: str, title: str, body: str) -> Report:
        report = Report(id=f"r-{uuid.uuid4().hex[:6]}", owner=owner,
                        conversation_id=conversation_id, title=title, body=body,
                        created_at=datetime.now(UTC).isoformat(timespec="seconds"))
        with self._connect() as con:
            con.execute("INSERT INTO reports VALUES (?, ?, ?, ?, ?, ?)",
                        (report.id, owner, conversation_id, title, body, report.created_at))
        return report

    def list(self, owner: str) -> list[Report]:
        return self.find(owner)

    def find(self, owner: str, *, mentions: str | None = None,
             conversation_id: str | None = None) -> list[Report]:
        sql = "SELECT * FROM reports WHERE owner = ?"
        params: list[str] = [owner]
        if mentions:
            sql += " AND (instr(lower(title), ?) > 0 OR instr(lower(body), ?) > 0)"
            params += [mentions.lower(), mentions.lower()]
        if conversation_id:
            sql += " AND conversation_id = ?"
            params.append(conversation_id)
        with self._connect() as con:
            rows = con.execute(sql + " ORDER BY created_at, rowid", params).fetchall()
        return [Report(**dict(row)) for row in rows]

    def delete(self, owner: str, ids: list[str]) -> int:
        if not ids:
            return 0
        placeholders = ", ".join("?" * len(ids))
        with self._connect() as con:
            cursor = con.execute(
                f"DELETE FROM reports WHERE owner = ? AND id IN ({placeholders})", [owner, *ids])
            return cursor.rowcount
