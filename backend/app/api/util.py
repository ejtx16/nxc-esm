"""Shared API helpers."""
from __future__ import annotations

from typing import Any

from app.data import db


def shape(row: dict | None, json_cols: tuple[str, ...] = ()) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    for c in json_cols:
        if c in out:
            out[c] = db.loads(out[c], [] if c.endswith("s") or c.endswith("ids") else {})
    return out


def shape_all(rows: list[dict], json_cols: tuple[str, ...] = ()) -> list[dict]:
    return [shape(r, json_cols) for r in rows]


def current_run() -> dict | None:
    from app.data import scenarios
    run = db.one("SELECT * FROM runs ORDER BY started_real DESC LIMIT 1")
    return {**run, "title": scenarios.title(run["scenario"])} if run else None


def page(limit: int | None, default: int = 100, cap: int = 1000) -> int:
    return max(1, min(limit or default, cap))
