"""Users of the assistant and the products each one is allowed to analyse."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, model_validator


class User(BaseModel):
    id: str
    name: str
    role: str
    all_products: bool = False
    brands: list[str] = []
    departments: list[str] = []

    @model_validator(mode="after")
    def _has_scope(self) -> User:
        if not (self.all_products or self.brands or self.departments):
            raise ValueError(f"User '{self.id}' has no product scope")
        return self

    def scope(self) -> str:
        if self.all_products:
            return "all products"
        parts = []
        if self.departments:
            parts.append("department " + " or ".join(self.departments))
        if self.brands:
            parts.append("brand " + " or ".join(self.brands))
        return " and ".join(parts)


def load_users(path: Path) -> dict[str, User]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {u["id"]: User(**u) for u in raw["users"]}
