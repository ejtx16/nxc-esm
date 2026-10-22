"""Self-service (D4) and knowledge base (D5) routes."""
from __future__ import annotations

import difflib
import time

from fastapi import APIRouter, Body, HTTPException

from app import settings
from app.api.util import page, shape, shape_all
from app.data import db
from app.runtime import bus
from app.runtime.clock import clock
from app.tools import mock_iam, mock_servicenow as snow

router = APIRouter()


def _sr(sr_id: str) -> dict | None:
    r = shape(db.one("SELECT * FROM service_requests WHERE sr_id=?", (sr_id,)), ("trace",))
    if r:
        r["trace"] = r["trace"] if isinstance(r["trace"], list) else []
    return r


@router.get("/employees")
def employees() -> list[dict]:
    return db.query("SELECT employee_id, name, role, department, status FROM employees "
                    "WHERE employee_id LIKE 'EMP1%' OR employee_id='EMP9999' ORDER BY employee_id")


@router.post("/self-service")
def self_service(body: dict = Body(...)) -> dict:
    """SR-1: portal channel. Structured form (request_type) or free text. Returns the fulfilment trace."""
    emp_id = (body.get("employee_id") or "").strip().upper()
    if not emp_id:
        raise HTTPException(400, "employee_id is required")
    text, rtype = (body.get("text") or "").strip() or None, body.get("request_type") or None
    if not text and not rtype:
        raise HTTPException(400, "provide a request type or describe the request")
    emp = mock_iam.lookup(emp_id)
    sid = snow.next_sr_id()
    db.insert("service_requests", {
        "sr_id": sid, "request_type": rtype, "requestor_id": emp_id, "department": emp["department"] if emp else None,
        "submitted_ts": clock.now_iso(), "priority": "Medium", "status": "New", "origin": "live", "channel": "portal",
        "text": text, "target_system": body.get("target_system"), "access_level": body.get("access_level"),
        "sla_target_hrs": snow.sr_sla_target(rtype) if rtype else None,
        "trace": db.dumps([{"ts": clock.now_iso(), "text": f"Request received via the self-service portal from {emp_id}"}])})
    eid = bus.publish(bus.SR_SUBMITTED, sid, {"channel": "portal"})
    deadline = time.time() + 30
    while time.time() < deadline:                       # the worker runs the graph; wait for this event to finish
        st = db.scalar("SELECT status FROM events WHERE event_id=?", (eid,))
        if st in ("done", "failed"):
            break
        time.sleep(0.03)
    return {"sr": _sr(sid), "approvals": shape_all(db.query("SELECT * FROM approvals WHERE entity_ref=? ORDER BY approval_id", (sid,)),
                                                    ("failed_rules", "reasons", "context", "result")),
            "decisions": shape_all(db.query("SELECT * FROM agent_decisions WHERE entity_ref=? ORDER BY decision_id", (sid,)),
                                   ("proposed_action",)),
            "surge_mode": bool(db.get_setting("surge_mode"))}


@router.get("/service-requests")
def service_requests(status: str | None = None, origin: str | None = "live", limit: int | None = None) -> list[dict]:
    where, args = ["1=1"], []
    if origin == "live":
        where.append("origin!='seed'")
    if status:
        where.append("status=?"); args.append(status)
    rows = db.query(f"SELECT * FROM service_requests WHERE {' AND '.join(where)} ORDER BY submitted_ts DESC, sr_id DESC LIMIT ?",
                    [*args, page(limit, 100, 1000)])
    return shape_all(rows, ("trace",))


@router.get("/service-requests/summary")
def sr_summary() -> dict:
    rows = db.query("SELECT status, channel, fulfilled_by, request_type, sla_met FROM service_requests WHERE origin!='seed'")
    done = [r for r in rows if r["status"] == "Completed"]
    return {"total": len(rows), "by_status": _count(rows, "status"), "by_type": _count(rows, "request_type"),
            "auto": sum(1 for r in done if (r["fulfilled_by"] or "").startswith("agent:")),
            "human": sum(1 for r in done if not (r["fulfilled_by"] or "").startswith("agent:")),
            "sla_met": sum(1 for r in done if r["sla_met"] == "Yes"), "completed": len(done),
            "surge_mode": bool(db.get_setting("surge_mode")),
            "opportunity": db.query("SELECT * FROM automation_opportunity ORDER BY avg_volume_per_month DESC")}


def _count(rows: list[dict], key: str) -> dict:
    out: dict = {}
    for r in rows:
        out[r[key] or "—"] = out.get(r[key] or "—", 0) + 1
    return out


@router.get("/service-requests/{sr_id}")
def sr_detail(sr_id: str) -> dict:
    r = _sr(sr_id)
    if r is None:
        raise HTTPException(404, "not found")
    return r


# --- knowledge base ---------------------------------------------------------------------------------
@router.get("/kb/articles")
def kb_articles() -> list[dict]:
    rows = db.query("SELECT article_id, title, category, last_updated, usage_count_90days, resolved_with_kb_count, "
                    "escalation_despite_kb, score, recommendation, issues, rationale, score_parts, updated_by_agent, "
                    "superseded_by, assessment_mode, systems FROM kb_articles ORDER BY score IS NULL, score, article_id")
    out = shape_all(rows, ("issues", "score_parts", "systems"))
    th = settings.cfg()["kb"]["accuracy_threshold"]
    pending = {d["article_id"]: d["draft_id"] for d in db.query("SELECT article_id, draft_id FROM kb_drafts WHERE status='pending'")}
    for a in out:
        a["below_threshold"] = a["score"] is not None and a["score"] < th
        a["pending_draft_id"] = pending.get(a["article_id"])
        a["seed_accuracy_pct"] = db.scalar("SELECT accuracy_score_pct FROM kb_articles WHERE article_id=?", (a["article_id"],))
    return out


@router.get("/kb/articles/{article_id}")
def kb_article(article_id: str) -> dict:
    a = shape(db.one("SELECT * FROM kb_articles WHERE article_id=?", (article_id,)), ("issues", "score_parts", "systems"))
    if a is None:
        raise HTTPException(404, "not found")
    drafts = []
    for d in db.query("SELECT * FROM kb_drafts WHERE article_id=? ORDER BY draft_id DESC", (article_id,)):
        d = shape(d, ("sources",))
        old, new = (a["body"] or "").splitlines(), d["new_body"].splitlines()
        d["diff"] = [{"op": l[0], "text": l[2:]} for l in difflib.ndiff(old, new) if l[0] in "+- "]
        d["approval"] = db.one("SELECT approval_id, status FROM approvals WHERE action='update_kb_article' AND "
                               "entity_ref=? ORDER BY approval_id DESC LIMIT 1", (article_id,))
        drafts.append(d)
    return {**a, "drafts": drafts}


@router.post("/kb/review")
def kb_review(body: dict = Body(default={})) -> dict:
    bus.publish(bus.KB_REVIEW, body.get("article_id", "ALL"), {})
    return {"queued": True}


@router.post("/kb/articles/{article_id}/draft")
def kb_draft(article_id: str) -> dict:
    if db.one("SELECT 1 FROM kb_articles WHERE article_id=?", (article_id,)) is None:
        raise HTTPException(404, "not found")
    bus.publish(bus.KB_REVIEW, article_id, {"draft": True})
    return {"queued": True}
