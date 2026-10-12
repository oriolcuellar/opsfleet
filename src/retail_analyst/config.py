"""Settings, read from environment variables (and the .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    google_api_key: str
    gcp_project: str
    model: str
    fallback_model: str
    max_bytes_billed: int
    max_sql_attempts: int
    data_dir: Path

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv(ROOT / ".env")
        data_dir = ROOT / os.getenv("DATA_DIR", ".data")
        data_dir.mkdir(parents=True, exist_ok=True)
        return cls(
            google_api_key=os.getenv("GOOGLE_API_KEY", ""),
            gcp_project=os.getenv("GOOGLE_CLOUD_PROJECT", ""),
            model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
            fallback_model=os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.1-flash-lite"),
            max_bytes_billed=int(os.getenv("BQ_MAX_BYTES_BILLED", 1_000_000_000)),
            max_sql_attempts=int(os.getenv("MAX_SQL_ATTEMPTS", 5)),
            data_dir=data_dir,
        )
