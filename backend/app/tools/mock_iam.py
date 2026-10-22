"""Mock IAM: identity, entitlement and fulfilment workflows (SR-2, SR-3)."""
from __future__ import annotations

import secrets

from app import settings
from app.data import db
from app.data.audit_ctx import audited_tool
from app.runtime.clock import clock


@audited_tool
def lookup(employee_id: str) -> dict | None:
    return db.one("SELECT * FROM employees WHERE employee_id=?", (employee_id.strip().upper(),))


@audited_tool
def is_privileged(system: str | None, level: str | None) -> bool:
    ent = settings.load_yaml("entitlements")
    return bool((level or "").lower() in ent["privileged_levels"] or system in ent["privileged_systems"])


@audited_tool
def entitled(role: str, system: str | None) -> bool:
    roles = settings.load_yaml("entitlements")["roles"]
    return bool(system and system in roles.get(role, {}).get("systems", []))


def reset_password(employee_id: str) -> dict:
    temp = secrets.token_urlsafe(9)                                  # never stored or logged in full
    db.insert("iam_log", {"ts": clock.now_iso(), "employee_id": employee_id, "action": "reset_password",
                          "system": "AuthService", "level": None, "detail": "temporary credential issued"})
    return {"ok": True, "credential_hint": temp[:2] + "*" * 7, "delivery": "secure channel to registered device",
            "rollback": "Invalidate the temporary credential"}


def grant_access(employee_id: str, system: str, level: str) -> dict:
    db.insert("iam_log", {"ts": clock.now_iso(), "employee_id": employee_id, "action": "grant_access",
                          "system": system, "level": level, "detail": "access granted"})
    return {"ok": True, "system": system, "level": level, "rollback": f"Revoke {level} access to {system}"}
