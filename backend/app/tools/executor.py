"""Executes an approved ActionProposal against the mock systems.

The same function runs for Guardrail-approved actions and for actions a human approves later,
so autonomous and human-approved paths cannot diverge.
"""
from __future__ import annotations

import time
from typing import Any, Callable

from app.data import audit_ctx, db
from app.runtime import bus
from app.runtime.clock import clock, iso, parse
from app.tools import mock_iam, mock_servicenow as snow, runbooks

Handler = Callable[[dict, str], dict]
HANDLERS: dict[str, Handler] = {}


def handler(name: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        HANDLERS[name] = fn
        return fn
    return deco


def execute(proposal: dict, actor: str) -> dict:
    fn = HANDLERS.get(proposal["action"])
    if fn is None:
        return {"ok": False, "message": f"no executor for action '{proposal['action']}'"}
    with audit_ctx.span("tool", f"executor.{proposal['action']}", proposal.get("agent"),
                        entity_ref=proposal.get("entity_ref"), actor=actor, side_effect=True,
                        params=proposal.get("params")) as sp:
        try:
            res = fn(proposal, actor)
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator, never crashes the worker
            res = {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
        sp.set_output(res)
        if not res.get("ok"):
            sp.set_status("error", res.get("message"))
        return res


# --- alerts / clusters ------------------------------------------------------------------------
@handler("acknowledge")
def _acknowledge(p: dict, actor: str) -> dict:
    cid = p["entity_ref"]
    c = db.one("SELECT * FROM clusters WHERE cluster_id=?", (cid,))
    if c["ack_ts"]:
        return {"ok": True, "message": f"{cid} already acknowledged"}
    now = clock.now()
    latency = max(0.0, (now - parse(c["first_alert_ts"])).total_seconds())
    event_real = p.get("params", {}).get("event_real") or c["created_real"]
    real_ms = (time.perf_counter() - event_real) * 1000
    db.update("clusters", "cluster_id", cid, {"status": "acknowledged", "ack_ts": iso(now),
                                              "ack_latency_s": round(latency, 1), "ack_real_ms": round(real_ms, 2)})
    db.execute("UPDATE alerts SET acknowledged='Yes', ack_time_mins=?, status='acknowledged' WHERE cluster_id=?",
               (round(latency / 60, 2), cid))
    return {"ok": True, "message": f"{cid} acknowledged {round(latency)}s (sim) after first alert",
            "ack_latency_s": round(latency, 1), "ack_real_ms": round(real_ms, 2)}


@handler("open_incident")
def _open_incident(p: dict, actor: str) -> dict:
    cid = p["entity_ref"]
    c = db.one("SELECT * FROM clusters WHERE cluster_id=?", (cid,))
    if c["incident_id"]:
        return {"ok": True, "message": f"{cid} already has {c['incident_id']}", "incident_id": c["incident_id"]}
    params = p["params"]
    inc_id = snow.next_incident_id()
    now = clock.now_iso()
    prio = params["provisional_priority"]
    db.insert("incidents", {
        "incident_id": inc_id, "category": None, "priority": prio, "provisional_priority": prio,
        "short_description": params["description"], "reported_by": "agent:alert_correlation",
        "reported_date": now[:10], "assigned_team": None, "assigned_agent": None, "status": "Open",
        "resolution_notes": "Awaiting triage", "origin": params.get("origin", "scenario"), "run_id": c["run_id"],
        "source_cluster_id": cid, "app_id": c["primary_system"], "sla_hours": snow.sla_hours(prio),
        "opened_ts": now, "escalated": 0, "held": 0})
    db.update("clusters", "cluster_id", cid, {"incident_id": inc_id})
    db.execute("UPDATE alerts SET incident_created='Yes', incident_id=? WHERE cluster_id=?", (inc_id, cid))
    bus.publish(bus.INCIDENT_CREATED, inc_id, {"cluster_id": cid, "kb_article_ids": params.get("kb_article_ids", [])},
                run_id=c["run_id"])
    return {"ok": True, "message": f"{inc_id} opened from {cid} ({prio})", "incident_id": inc_id}


@handler("suppress")
def _suppress(p: dict, actor: str) -> dict:
    cid = p["entity_ref"]
    db.update("clusters", "cluster_id", cid, {"status": "suppressed"})
    db.execute("UPDATE alerts SET status='suppressed' WHERE cluster_id=?", (cid,))
    snow.record_outcome("suppress", cid)
    return {"ok": True, "message": f"{cid} suppressed (reversible - expires after review)"}


# --- incidents ----------------------------------------------------------------------------------
@handler("classify")
def _classify(p: dict, actor: str) -> dict:
    inc_id, a = p["entity_ref"], p["params"]
    db.update("incidents", "incident_id", inc_id, {
        "category": a["category"], "priority": a["priority"], "sla_hours": snow.sla_hours(a["priority"]),
        "confidence": a["confidence"], "rationale": a["rationale"], "similar_ids": db.dumps(a["similar_ids"]),
        "auto_resolvable": 1 if a["auto_resolvable"] else 0, "runbook_id": a.get("runbook_id"),
        "triaged_ts": clock.now_iso(), "triage_ms": a["triage_ms"], "triage_mode": a["mode"],
        "app_id": a.get("app_id")})
    return {"ok": True, "message": f"{inc_id} classified {a['category']} / {a['priority']}"}


@handler("route")
def _route(p: dict, actor: str) -> dict:
    inc_id, team = p["entity_ref"], p["params"]["team"]
    inc = db.one("SELECT status FROM incidents WHERE incident_id=?", (inc_id,))
    status = "Assigned" if inc["status"] in ("Open", "Held") else inc["status"]
    db.update("incidents", "incident_id", inc_id, {"assigned_team": team, "assigned_agent": "agent:incident_triage",
                                                   "status": status, "held": 0})
    return {"ok": True, "message": f"{inc_id} routed to {team}"}


@handler("attach_kb")
def _attach_kb(p: dict, actor: str) -> dict:
    db.update("incidents", "incident_id", p["entity_ref"], {"kb_article_ids": db.dumps(p["params"]["article_ids"])})
    return {"ok": True, "message": f"attached {', '.join(p['params']['article_ids']) or 'no KB articles'}"}


@handler("escalate")
def _escalate(p: dict, actor: str) -> dict:
    inc_id, team = p["entity_ref"], p["params"].get("team")
    db.update("incidents", "incident_id", inc_id, {"escalated": 1, "escalated_ts": clock.now_iso(),
                                                   "status": "Escalated"})
    return {"ok": True, "message": f"paged on-call for {team or 'owning team'} ({p['params'].get('reason', '')})"}


@handler("hold_reassignment")
def _hold(p: dict, actor: str) -> dict:
    db.update("incidents", "incident_id", p["entity_ref"], {"held": 1, "status": "Held"})
    return {"ok": True, "message": "non-urgent reassignment held during P1 surge"}


@handler("resolve")
def _resolve(p: dict, actor: str) -> dict:
    inc_id, rb_id = p["entity_ref"], p["params"]["runbook_id"]
    inc = db.one("SELECT * FROM incidents WHERE incident_id=?", (inc_id,))
    result = runbooks.run(rb_id)
    now = clock.now()
    hrs = max(0.0, (now - parse(inc["opened_ts"] or inc["reported_date"])).total_seconds() / 3600)
    notes = f"Auto-resolved via {rb_id} ({result['name']}): " + "; ".join(result["steps_run"])
    db.update("incidents", "incident_id", inc_id, {
        "status": "Resolved", "assigned_agent": "Resolver agent" if actor == "guardrail" else actor,
        "resolution_time_hrs": round(hrs, 3), "resolution_notes": notes,
        "sla_breach": "Yes" if inc["sla_hours"] and hrs > inc["sla_hours"] else "No"})
    snow.record_outcome("resolve", inc_id)
    # Close the alert ticket the Alert agent handed off, so the log shows the whole loop: alert -> incident -> fix.
    cid = inc["source_cluster_id"]
    n_alerts = db.scalar("SELECT COUNT(*) FROM alerts WHERE cluster_id=?", (cid,), default=0) if cid else 0
    if cid:
        db.update("clusters", "cluster_id", cid, {"status": "resolved"})
        db.execute("UPDATE alerts SET status='resolved', auto_resolved='Yes', resolution_notes=? WHERE cluster_id=?",
                   (notes, cid))
    kb = (p.get("context") or {}).get("kb_article")
    deadline = (f"inside its {inc['sla_hours']:g} h deadline" if inc["sla_hours"] and hrs <= inc["sla_hours"]
                else f"past its {inc['sla_hours']:g} h deadline" if inc["sla_hours"] else "with no deadline set")
    handoff = ("The Guardrail rated it low risk and delegated the fix to the Resolver agent"
               if actor == "guardrail" else f"{actor} approved the fix and the Resolver agent carried it out")
    message = (f"{inc_id} is resolved. {handoff}, which ran runbook {rb_id} ({result['name']})"
               + (f" following KB article {kb}" if kb else "")
               + ". Steps: " + "; ".join(result["steps_run"])
               + f". It took {hrs * 60:.0f} min from opening, {deadline}."
               + (f" Closed alert group {cid} and all {n_alerts} alert(s) in it that the Alert agent handed off."
                  if cid else ""))
    to = [r for r in (inc["assigned_team"], inc["reported_by"]) if r]
    note = snow.notify("resolved", inc_id, to, message)
    message += f" Sent a resolution notice ({note['channel']}) to {', '.join(to) or 'nobody: no owner on record'}."
    open_p1 = db.scalar("SELECT COUNT(*) FROM incidents WHERE priority='P1' AND status NOT IN ('Resolved','Closed') "
                        "AND origin!='seed'", default=0)
    if inc["priority"] == "P1" and open_p1 == 0 and db.get_setting("surge_mode"):
        bus.publish(bus.SR_SURGE_OFF, inc_id, {"reason": "last P1 resolved"}, run_id=inc["run_id"])
    return {"ok": True, "message": message, "steps": result["steps_run"], "alerts_closed": n_alerts, "notification": note,
            "rollback": result["rollback"]}


# --- service requests ---------------------------------------------------------------------------
@handler("route_to_human")
def _route_to_human(p: dict, actor: str) -> dict:
    sr_id = p["entity_ref"]
    db.update("service_requests", "sr_id", sr_id, {"status": "Pending Human"})
    snow.append_sr_trace(sr_id, f"Routed to human approver/queue: {p['params'].get('reason', '')}")
    return {"ok": True, "message": f"{sr_id} routed to the human queue"}


def _per_kb(params: dict) -> str:
    """' (per KB4001)' when the SR agent fulfilled the request from a knowledge document."""
    return f" (per {params['kb_article']})" if params.get("kb_article") else ""


def _trace_kb(sr_id: str, params: dict) -> None:
    if params.get("kb_article"):
        steps = "; ".join(params.get("kb_steps") or []) or "procedure applied"
        snow.append_sr_trace(sr_id, f"Followed the solution in {params['kb_article']}: {steps}")


@handler("reset_password")
def _reset_password(p: dict, actor: str) -> dict:
    sr_id, emp = p["entity_ref"], p["params"]["employee_id"]
    _trace_kb(sr_id, p["params"])
    res = mock_iam.reset_password(emp)
    done = snow.close_sr(sr_id, f"agent:sr_fulfilment" if actor.startswith("guardrail") else f"approved:{actor}",
                         "password reset" + _per_kb(p["params"]))
    snow.append_sr_trace(sr_id, f"Password reset issued ({res['credential_hint']}) via {res['delivery']}")
    snow.record_outcome("reset_password", sr_id)
    return {"ok": True, "message": f"password reset for {emp}{_per_kb(p['params'])}", **res, **done}


@handler("grant_access")
def _grant_access(p: dict, actor: str) -> dict:
    sr_id, a = p["entity_ref"], p["params"]
    _trace_kb(sr_id, a)
    res = mock_iam.grant_access(a["employee_id"], a["system"], a["level"])
    done = snow.close_sr(sr_id, f"agent:sr_fulfilment" if actor.startswith("guardrail") else f"approved:{actor}",
                         f"{a['level']} access to {a['system']}" + _per_kb(a))
    snow.append_sr_trace(sr_id, f"Granted {a['level']} access to {a['system']}")
    snow.record_outcome("grant_access", sr_id)
    return {"ok": True, "message": f"{a['level']} access to {a['system']} for {a['employee_id']}{_per_kb(a)}", **res, **done}


# --- knowledge base / fallback ------------------------------------------------------------------
@handler("update_kb_article")
def _update_kb(p: dict, actor: str) -> dict:
    from app.agents import kb_curation
    return kb_curation.apply_draft(p["params"]["draft_id"], actor)


@handler("manual_review")
def _manual_review(p: dict, actor: str) -> dict:
    return {"ok": True, "message": f"operator {actor} acknowledged manual review of {p['entity_ref']}"}
