"""Paths, model constants and YAML config loading."""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any

import yaml
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parents[1]          # backend/
REPO_DIR = BASE_DIR.parent
load_dotenv(BASE_DIR / ".env")

CONFIG_DIR = BASE_DIR / "config"
DATA_DIR = BASE_DIR / "data"
FILES_DIR = Path(os.getenv("NEXA_FILES_DIR", REPO_DIR / "files"))
GOLDEN_PATH = DATA_DIR / "golden" / "triage_labels.csv"
CURATED_DIR = DATA_DIR / "curated"            # Excel-derived answer key for the Excel accuracy check

MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def db_path() -> Path:
    return Path(os.getenv("NEXA_DB_PATH", DATA_DIR / "nexacore.db"))


_cache: dict[str, tuple[float, Any]] = {}


def load_yaml(name: str) -> Any:
    """Load config/<name>.yaml, re-reading when the file changes (policy is config, GR-3)."""
    path = CONFIG_DIR / f"{name}.yaml"
    mtime = path.stat().st_mtime
    hit = _cache.get(name)
    if hit and hit[0] == mtime:
        return hit[1]
    with path.open() as fh:
        data = yaml.safe_load(fh)
    _cache[name] = (mtime, data)
    return data


def cfg() -> dict:
    return load_yaml("settings")


def env_llm_mode() -> str:
    return os.getenv("LLM_MODE", "live").strip().lower()


CASE_START = datetime(2025, 8, 4, 8, 55)      # the case-study calendar; tests and the KPI simulation pin to it


def sim_start() -> datetime:
    """Naive local wall-clock start of the simulation. `sim.start: now` uses `sim.timezone` (Asia/Manila);
    NEXA_SIM_START (ISO) pins it, which keeps tests and the KPI simulation reproducible."""
    pinned = os.getenv("NEXA_SIM_START")
    if pinned:
        return datetime.fromisoformat(pinned)
    sim = cfg()["sim"]
    if str(sim.get("start", "now")).lower() == "now":
        return datetime.now(ZoneInfo(sim.get("timezone", "Asia/Manila"))).replace(tzinfo=None, second=0, microsecond=0)
    return datetime.fromisoformat(str(sim["start"]))


def date_shift() -> timedelta:
    """Whole days between the case calendar and the simulation start; historic data is moved by this much."""
    return timedelta(days=(sim_start().date() - CASE_START.date()).days)
