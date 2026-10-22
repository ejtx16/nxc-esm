"""Human decisions on the Guardrail queue (GR-4). Approving runs the same executor the Guardrail uses."""
from __future__ import annotations

from app.data import audit, audit_ctx, db
from app.runtime.clock import clock, parse
from app.tools import executor


def _row(approval_id: int) -> dict | None:
    return db.one("SELECT * FROM approvals WHERE approval_id=?", (approval_id,))


def decide(approval_id: int, approve: bool, decided_by: str = "operator", note: str = "") -> dict:
    a = _row(approval_id)
    if a is None:
        return {"ok": False, "status": 404, "message": "approval not found"}
    if a["status"] not in ("pending", "blocked"):
        return {"ok": False, "status": 409, "message": f"approval already {a['status']}"}
    if approve and a["status"] == "blocked":
        return {"ok": False, "status": 409, "message": "blocked by policy (irreversible on Tier 0) - it can only be rejected"}
    with audit_ctx.start_trace(run_id=a["run_id"], entity_ref=a["entity_ref"]), \
            audit_ctx.span("human", "approve" if approve else "reject", "human", entity_ref=a["entity_ref"],
                           approval_id=approval_id, action=a["action"], decided_by=decided_by) as sp:
        out = _decide(a, approve, decided_by, note)
        sp.set_output({"state": out["state"], "ok": out["ok"], "wait_s": out.pop("wait_s", None)})
    return out


def _decide(a: dict, approve: bool, decided_by: str, note: str) -> dict:
    approval_id = a["approval_id"]
    proposal = db.loads(a["proposal"], {})
    if approve:
        res = executor.execute(proposal, decided_by)
        status = "executed" if res.get("ok") else "failed"
    else:
        res = {"ok": True, "message": "rejected"}
        status = "rejected"
        _on_reject(proposal)
    db.update("approvals", "approval_id", approval_id, {
        "status": status, "decided_ts": clock.now_iso(), "decided_by": decided_by, "note": note,
        "result": db.dumps(res)})
    hid = audit.log("human", "human", a["entity_ref"], input_summary=a["summary"],
                    reasoning=note or (res.get("message") or ""), human_decision="approved" if approve else "rejected",
                    verdict=a["verdict"], verdict_reason=a["failed_rules"], parent_id=a["decision_id"],
                    run_id=a["run_id"], proposed_action={"action": a["action"], "ok": res.get("ok")})
    if approve and res.get("ok"):
        audit.log("execution", proposal.get("agent", "human"), a["entity_ref"], reasoning=res.get("message", ""),
                  input_summary=a["summary"], parent_id=hid, run_id=a["run_id"],
                  proposed_action={"action": a["action"], "ok": True})
    _record_override(a, proposal, approve)
    try:
        wait_s = round((parse(clock.now_iso()) - parse(a["created_ts"])).total_seconds(), 1)
    except (TypeError, ValueError):
        wait_s = None
    return {"ok": res.get("ok", False), "status": 200, "approval_id": approval_id, "result": res,
            "state": status, "wait_s": wait_s}


def _record_override(a: dict, proposal: dict, approve: bool) -> None:
    """Accuracy from the human outcome: approving the agent's proposal counts as correct (AU-4)."""
    agent_decision = db.scalar("SELECT parent_id FROM agent_decisions WHERE decision_id=?", (a["decision_id"],))
    audit_ctx.record_accuracy(
        proposal.get("agent") or a["agent"] or "unknown", f"proposal:{a['action']}",
        predicted=a["action"], expected=a["action"] if approve else "rejected", correct=approve,
        confidence=proposal.get("confidence"), source="human_override", decision_id=agent_decision or a["decision_id"],
        run_id=a["run_id"])


def _on_reject(proposal: dict) -> None:
    action = proposal.get("action")
    if action == "suppress":
        db.update("clusters", "cluster_id", proposal["entity_ref"], {"status": "surfaced"})
        db.execute("UPDATE alerts SET status='surfaced' WHERE cluster_id=?", (proposal["entity_ref"],))
    elif action == "update_kb_article":
        from app.agents import kb_curation
        kb_curation.reject_draft(proposal["params"]["draft_id"])
    elif action in ("grant_access", "reset_password", "route_to_human"):
        db.update("service_requests", "sr_id", proposal["entity_ref"],
                  {"status": "Rejected", "outcome": "rejected by approver"})
