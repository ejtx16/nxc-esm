"""Alerts, clusters, incidents and the guardrail approval queue (D1-D3)."""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException

from app import settings
from app.agents import incident_triage
from app.api.util import current_run, page, shape, shape_all
from app.data import db
from app.runtime import approvals as appr, bus, engine
from app.runtime.clock import clock, parse
from app.tools import kb_store, mock_servicenow as snow

router = APIRouter()


def _run_filter(run_id: str | None) -> tuple[str, list]:
    if run_id == "all":
        return "1=1", []
    rid = run_id or (current_run() or {}).get("run_id")
    return ("run_id=?", [rid]) if rid else ("1=0", [])


@router.get("/alerts")
def alerts(run_id: str | None = None, cluster_id: str | None = None, limit: int | None = None,
           since: str | None = None) -> list[dict]:
    where, args = _run_filter(run_id)
    if cluster_id:
        where += " AND cluster_id=?"; args.append(cluster_id)
    rows = db.query(f"SELECT * FROM alerts WHERE {where} ORDER BY alert_ts DESC, alert_id DESC LIMIT ?", [*args, page(limit, 120, 2000)])
    return rows


@router.get("/clusters")
def clusters(run_id: str | None = None, status: str | None = None) -> list[dict]:
    where, args = _run_filter(run_id)
    if status:
        where += " AND status=?"; args.append(status)
    rows = db.query(f"SELECT * FROM clusters WHERE {where} ORDER BY first_alert_ts DESC, cluster_id DESC", args)
    out = shape_all(rows, ("systems",))
    for c in out:
        c["tier"] = snow.app_tier(c["primary_system"])
    return out


@router.get("/live-feed")
def live_feed(run_id: str | None = None) -> dict:
    """One call for the Live Feed page: counters, clusters, newest alerts."""
    where, args = _run_filter(run_id)
    cl = clusters(run_id)
    raw = db.scalar(f"SELECT COUNT(*) FROM alerts WHERE {where}", args, default=0)
    srs = shape_all(db.query(f"SELECT * FROM service_requests WHERE {where} ORDER BY submitted_ts DESC, sr_id DESC LIMIT 40", args),
                    ("trace",))
    for r in srs:
        r["trace"] = r["trace"] if isinstance(r["trace"], list) else []
    return {"raw_alerts": raw, "clusters": cl[:60], "cluster_count": len(cl),
            "alerts": alerts(run_id, limit=40), "service_requests": srs,
            "sr_count": db.scalar(f"SELECT COUNT(*) FROM service_requests WHERE {where}", args, default=0),
            "status_counts": {r["status"]: r["n"] for r in db.query(
                f"SELECT status, COUNT(*) n FROM clusters WHERE {where} GROUP BY status", args)},
            "sim_now": clock.now_iso()}


def _sla(inc: dict) -> dict:
    window = inc["sla_hours"] or snow.sla_hours(inc["priority"])
    if inc["status"] in ("Resolved", "Closed"):
        return {"window_h": window, "elapsed_h": inc["resolution_time_hrs"], "remaining_h": None, "risk": None,
                "breached": inc["sla_breach"] == "Yes"}
    opened = parse(inc["opened_ts"] or inc["reported_date"])
    elapsed = max(0.0, (clock.now() - opened).total_seconds() / 3600)
    return {"window_h": window, "elapsed_h": round(elapsed, 3), "remaining_h": round(window - elapsed, 3),
            "risk": round(elapsed / window, 3) if window else None, "breached": elapsed > window}


@router.get("/incidents")
def incidents(origin: str | None = "live", status: str | None = None, limit: int | None = None) -> list[dict]:
    where, args = ["1=1"], []
    if origin == "live":
        where.append("origin!='seed'")
    elif origin == "seed":
        where.append("origin='seed'")
    if status == "open":
        where.append("status NOT IN ('Resolved','Closed')")
    rows = db.query(f"SELECT * FROM incidents WHERE {' AND '.join(where)} ORDER BY "
                    "CASE priority WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 WHEN 'P3' THEN 3 ELSE 4 END, incident_id DESC LIMIT ?",
                    [*args, page(limit, 200, 1000)])
    out = shape_all(rows, ("similar_ids", "kb_article_ids"))
    for r in out:
        r["sla"] = _sla(r)
        r["tier"] = snow.app_tier(r["app_id"])
        r["pending_approvals"] = db.scalar("SELECT COUNT(*) FROM approvals WHERE entity_ref=? AND status='pending'",
                                           (r["incident_id"],), default=0)
    return out


@router.get("/incidents/{incident_id}")
def incident(incident_id: str) -> dict:
    inc = shape(db.one("SELECT * FROM incidents WHERE incident_id=?", (incident_id,)), ("similar_ids", "kb_article_ids"))
    if inc is None:
        raise HTTPException(404, "incident not found")
    cluster = shape(db.one("SELECT * FROM clusters WHERE cluster_id=?", (inc["source_cluster_id"],)), ("systems",)) if inc["source_cluster_id"] else None
    similar = [db.one("SELECT incident_id, short_description, category, priority, assigned_team FROM incidents WHERE incident_id=?", (i,))
               for i in inc["similar_ids"] or []]
    kb = [shape(db.one("SELECT article_id, title, score, recommendation FROM kb_articles WHERE article_id=?", (a,))) for a in inc["kb_article_ids"] or []]
    text = inc["short_description"]
    _, excluded = kb_store.search(text, k=3, app_id=inc["app_id"])
    return {**inc, "sla": _sla(inc), "tier": snow.app_tier(inc["app_id"]), "cluster": cluster,
            "similar": [s for s in similar if s], "kb": kb,
            "kb_excluded": [{"article_id": a["article_id"], "title": a["title"], "score": a["score"]} for a in excluded],
            "decisions": shape_all(db.query("SELECT * FROM agent_decisions WHERE entity_ref IN (?,?) ORDER BY decision_id",
                                            (incident_id, inc["source_cluster_id"] or "-")), ("proposed_action",)),
            "approvals": shape_all(db.query("SELECT * FROM approvals WHERE entity_ref=? ORDER BY approval_id", (incident_id,)),
                                   ("failed_rules", "reasons", "proposal", "context", "result"))}


@router.post("/incidents/{incident_id}/reopen")
def reopen(incident_id: str) -> dict:
    """Simulate a reopen of an auto-resolved incident; feeds the Guardrail's reopen-rate rule (GR-6 input)."""
    inc = db.one("SELECT * FROM incidents WHERE incident_id=?", (incident_id,))
    if inc is None:
        raise HTTPException(404, "incident not found")
    db.update("incidents", "incident_id", incident_id, {"status": "Open"})
    snow.record_outcome("resolve", incident_id, "reopened")
    return {"ok": True}


@router.post("/sim/advance")
def advance(body: dict = Body(...)) -> dict:
    """Jump the sim clock forward (demo: show SLA-risk escalation without waiting)."""
    minutes = float(body.get("minutes", 0))
    clock.advance(minutes * 60)
    n = engine.sla_scan()
    return {"sim_now": clock.now_iso(), "sla_checks_queued": n}


@router.get("/approvals")
def approvals(status: str | None = None, limit: int | None = None) -> list[dict]:
    where, args = "1=1", []
    if status == "open":
        where = "status IN ('pending','blocked')"
    elif status:
        where, args = "status=?", [status]
    rows = db.query(f"SELECT * FROM approvals WHERE {where} ORDER BY approval_id DESC LIMIT ?", [*args, page(limit, 200, 1000)])
    out = shape_all(rows, ("failed_rules", "reasons", "proposal", "context", "result"))
    for a in out:
        a["tier"] = (a["context"] or {}).get("app_tier")
    return out


@router.post("/approvals/{approval_id}/decide")
def decide(approval_id: int, body: dict = Body(...)) -> dict:
    res = appr.decide(approval_id, bool(body.get("approve")), body.get("decided_by", "operator"), body.get("note", ""))
    if not res["ok"] and res.get("status", 200) != 200:
        raise HTTPException(res["status"], res["message"])
    bus.wake()
    return res
