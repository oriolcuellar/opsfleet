"""Last line of PII defence: mask anything that looks like personal data.

The SQL rewrite already removes PII columns. These functions are a safety net on the query
results (before the model sees them) and on the final answer (before the user sees it).
"""

from __future__ import annotations

import re
from typing import Any

from retail_analyst.sql_safety import PII_COLUMNS

_PATTERNS = {
    "EMAIL": re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),
    "PHONE": re.compile(r"(?<![\w.,$])(?:\+\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b"),
}


def mask_text(text: str) -> tuple[str, int]:
    """Return the masked text and how many values were masked."""
    total = 0
    for label, pattern in _PATTERNS.items():
        text, count = pattern.subn(f"[{label} REMOVED]", text)
        total += count
    return text, total


def clean_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cleaned = []
    for row in rows:
        out = {}
        for column, value in row.items():
            if column.lower() in PII_COLUMNS:
                continue
            out[column] = mask_text(value)[0] if isinstance(value, str) else value
        cleaned.append(out)
    return cleaned
