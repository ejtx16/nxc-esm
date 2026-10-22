"""Mock ServiceNow: incident/SR/alert record operations used by the executor."""
from __future__ import annotations

from datetime import datetime

from app import settings
from app.data import db
from app.data.audit_ctx import audited_tool, span
from app.runtime.clock import clock, iso, parse


def next_incident_id() -> str:
    n = db.scalar("SELECT MAX(CAST(SUBSTR(incident_id,4) AS INTEGER)) FROM incidents WHERE origin!='seed'", default=2000)
    return f"INC{max(int(n), 2000) + 1}"


def next_alert_id() -> str:
    n = db.scalar("SELECT MAX(CAST(SUBSTR(alert_id,4) AS INTEGER)) FROM alerts WHERE origin!='seed'", default=9000)
    return f"ALT{max(int(n), 9000) + 1}"


def next_sr_id() -> str:
    n = db.scalar("SELECT MAX(CAST(SUBSTR(sr_id,3) AS INTEGER)) FROM service_requests WHERE origin!='seed'", default=3000)
    return f"SR{max(int(n), 3000) + 1}"


def next_cluster_id() -> str:
    n = db.scalar("SELECT COUNT(*) FROM clusters", default=0)
    return f"CL-{int(n) + 1:04d}"


@audited_tool
def sla_hours(priority: str | None) -> float:
    return float(settings.cfg()["sla_hours"].get(priority or "P3", 24))


@audited_tool
def app_tier(app_id: str | None) -> int | None:
    if not app_id:
        return None
    return db.scalar("SELECT tier FROM apps WHERE app_id=?", (app_id,))


@audited_tool
def app_info(app_id: str | None) -> dict | None:
    return db.one("SELECT * FROM apps WHERE app_id=?", (app_id,)) if app_id else None


def record_outcome(action: str, entity_ref: str, outcome: str = "ok") -> None:
    db.insert("action_outcomes", {"action": action, "entity_ref": entity_ref, "ts": clock.now_iso(), "outcome": outcome})


@audited_tool
def reopen_rate_pct(action: str, window_days: int) -> float | None:
    since = iso(clock.now().replace(microsecond=0) - __import__("datetime").timedelta(days=window_days))
    rows = db.query("SELECT outcome FROM action_outcomes WHERE action=? AND ts>=?", (action, since))
    if not rows:
        return None
    return 100.0 * sum(1 for r in rows if r["outcome"] == "reopened") / len(rows)


@audited_tool
def sr_sla_target(request_type: str | None) -> float:
    v = db.scalar("SELECT AVG(sla_target_hrs) FROM service_requests WHERE request_type=? AND origin='seed'",
                  (request_type,))
    return float(round(v)) if v else 24.0


def close_sr(sr_id: str, fulfilled_by: str, outcome: str) -> dict:
    sr = db.one("SELECT * FROM service_requests WHERE sr_id=?", (sr_id,))
    now = clock.now()
    hrs = max(0.0, (now - parse(sr["submitted_ts"])).total_seconds() / 3600)
    target = sr["sla_target_hrs"] or sr_sla_target(sr["request_type"])
    db.update("service_requests", "sr_id", sr_id, {
        "status": "Completed", "fulfillment_time_hrs": round(hrs, 4), "sla_target_hrs": target,
        "sla_met": "Yes" if hrs <= target else "No", "fulfilled_by": fulfilled_by, "outcome": outcome,
        "completed_ts": iso(now)})
    return {"fulfillment_time_hrs": round(hrs, 4), "sla_met": hrs <= target}


def notify(kind: str, inc_id: str, recipients: list[str], body: str) -> dict:
    """Dummy notification (kind: resolved | assigned): nothing leaves the app; it is recorded as a tool span in the
    audit trail, which the console reads back to show a pop-up."""
    note = {"channel": "mock email", "kind": kind, "to": recipients, "subject": f"[{kind.title()}] {inc_id}",
            "body": body, "sent_ts": clock.now_iso()}
    with span("tool", f"mock_servicenow.notify_{kind}", entity_ref=inc_id, side_effect=True, to=recipients) as sp:
        sp.set_output(note)
    return note


def append_sr_trace(sr_id: str, line: str) -> None:
    sr = db.one("SELECT trace FROM service_requests WHERE sr_id=?", (sr_id,))
    trace = db.loads(sr["trace"], []) if sr else []
    trace.append({"ts": clock.now_iso(), "text": line})
    db.update("service_requests", "sr_id", sr_id, {"trace": db.dumps(trace)})
