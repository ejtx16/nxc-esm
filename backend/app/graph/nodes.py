"""LangGraph nodes: route_event, guardrail, execute, human_queue, finalize, p1_surge (docs/planning/implementation-plan.md §3)."""
from __future__ import annotations

import time
from typing import Callable

from app.agents import alert_correlation, guardrail as gr, incident_triage, kb_curation, sr_fulfilment
from app.agents.common import log_decision
from app.data import audit, audit_ctx, db
from app.graph.state import ActionProposal, OpsState
from app.runtime import bus
from app.runtime.clock import clock
from app.tools import executor, mock_servicenow as snow

ROUTES: dict[str, str] = {
    bus.ALERT_RECEIVED: "alert_correlation",
    bus.INCIDENT_CREATED: "incident_triage",
    bus.INCIDENT_PRIORITY_CHANGED: "incident_triage",
    bus.INCIDENT_SLA_CHECK: "incident_triage",
    bus.TRIAGE_HOLD: "incident_triage",
    bus.TRIAGE_RELEASE: "incident_triage",
    bus.SR_SUBMITTED: "sr_fulfilment",
    bus.SR_SURGE_ON: "sr_fulfilment",
    bus.KB_REVIEW: "kb_curation",
    bus.KB_VALIDATE_RELATED: "kb_curation",
    bus.SR_SURGE_OFF: "p1_surge",
}


def route_event(state: OpsState) -> dict:
    ev = state["event"]
    target = ROUTES.get(ev["type"], "finalize")
    return {"entity_ref": ev["entity_ref"], "trace": [f"orchestrator: {ev['type']} {ev['entity_ref']} -> {target}"]}


def route_target(state: OpsState) -> str:
    return ROUTES.get(state["event"]["type"], "finalize")


def safe(agent: str, fn: Callable[[OpsState], dict]) -> Callable[[OpsState], dict]:
    """OR-4: any exception or LLM timeout becomes a manual-review item for the human queue."""
    def node(state: OpsState) -> dict:
        try:
            return fn(state)
        except Exception as exc:  # noqa: BLE001
            ev = state["event"]
            msg = f"{agent} failed on {ev['type']} {ev['entity_ref']}: {type(exc).__name__}: {exc}"
            p = ActionProposal(agent=agent, action="manual_review", entity_ref=ev["entity_ref"],
                               summary=f"Handle {ev['entity_ref']} manually - {agent} failed",
                               params={"reason": "agent_failure", "error": str(exc), "event_type": ev["type"]})
            log_decision(state, agent, ev["entity_ref"], "AGENT FAILURE -> manual process: " + msg,
                         input_summary=ev["type"], confidence=0.0, proposals=[p], mode="error")
            return {"proposals": [p], "trace": [msg]}
    return node


def audited(name: str, fn: Callable[[OpsState], dict], agent: str | None = None) -> Callable[[OpsState], dict]:
    """Record every node run as a `node` span (iteration = step number in the trace; ADR-0004)."""
    def node(state: OpsState) -> dict:
        ev = state["event"]
        with audit_ctx.span("node", name, agent or name, entity_ref=state.get("entity_ref") or ev["entity_ref"],
                            event_type=ev["type"], proposals_in=len(state.get("proposals", []))) as sp:
            out = fn(state)
            if name in AGENT_NAMES and any(p.action == "manual_review" and p.params.get("reason") == "agent_failure"
                                            for p in out.get("proposals", [])):
                sp.set_status("error", "agent failure -> manual review")
            sp.set_output({k: len(v) for k, v in out.items() if isinstance(v, list)} or None)
            return out
    return node


def guardrail(state: OpsState) -> dict:
    ev = state["event"]
    verdicts, trace = [], []
    for p in state.get("proposals", []):
        with audit_ctx.span("guardrail", f"evaluate:{p.action}", "guardrail", entity_ref=p.entity_ref,
                            action=p.action, proposed_by=p.agent, confidence=p.confidence) as sp:
            v = gr.evaluate(p)
            sp.set_output({"verdict": v.verdict, "failed": v.failed_rules})
            v.decision_id = audit.log(
                "guardrail", "guardrail", p.entity_ref, input_summary=p.summary, confidence=p.confidence,
                reasoning=f"{v.risk} risk: " + "; ".join(v.reasons), proposed_action={"action": p.action, "agent": p.agent},
                verdict=v.verdict, verdict_reason=db.dumps({"failed": v.failed_rules, "passed": v.passed_rules,
                                                            "policy_version": v.policy_version, "risk": v.risk}),
                parent_id=p.parent_decision_id, run_id=ev.get("run_id"), event_id=ev.get("event_id"))
        verdicts.append(v)
        if p.action in SR_ACTIONS:                                  # the requester sees the risk call on their ticket
            snow.append_sr_trace(p.entity_ref, f"Guardrail: {v.risk} risk - "
                                 + ("approved to run without a person" if v.verdict == "Approve"
                                    else "needs a human approver") + f" ({'; '.join(v.reasons)})")
        if v.verdict != "Approve":
            trace.append(f"guardrail: {v.verdict} ({v.risk} risk) for '{p.summary}' - {'; '.join(v.reasons)}")
    return {"verdicts": verdicts, "trace": trace}


SR_ACTIONS = ("reset_password", "grant_access")


def _pairs(state: OpsState):
    by_id = {v.proposal_id: v for v in state.get("verdicts", [])}
    return [(p, by_id[p.proposal_id]) for p in state.get("proposals", []) if p.proposal_id in by_id]


def after_guardrail(state: OpsState) -> str:
    verdicts = state.get("verdicts", [])
    if any(v.verdict == "Approve" for v in verdicts):
        return "execute"
    return "human_queue" if verdicts else "finalize"


def execute(state: OpsState) -> dict:
    ev = state["event"]
    results, trace = [], []
    for p, v in _pairs(state):
        if v.verdict != "Approve":
            continue
        res = executor.execute(p.model_dump(), "guardrail")
        res = {**res, "action": p.action, "entity_ref": p.entity_ref, "proposal_id": p.proposal_id}
        audit.log("execution", p.agent, p.entity_ref, reasoning=res.get("message", ""), input_summary=p.summary,
                  verdict="Approve", human_decision=None, parent_id=v.decision_id, confidence=p.confidence,
                  run_id=ev.get("run_id"), event_id=ev.get("event_id"), proposed_action={"action": p.action, "ok": res.get("ok")})
        results.append(res)
        if p.action not in ("classify", "attach_kb"):
            trace.append(f"executed {p.action}: {res.get('message', '')}")
    return {"results": results, "trace": trace}


def after_execute(state: OpsState) -> str:
    return "human_queue" if any(v.verdict != "Approve" for v in state.get("verdicts", [])) else "finalize"


def human_queue(state: OpsState) -> dict:
    ev = state["event"]
    queued, trace = [], []
    for p, v in _pairs(state):
        if v.verdict == "Approve":
            continue
        dup = db.scalar("SELECT approval_id FROM approvals WHERE status='pending' AND action=? AND entity_ref=?",
                        (p.action, p.entity_ref))
        if dup:                                                # never stack identical pending approvals
            trace.append(f"human_queue: {p.action} for {p.entity_ref} already pending (#{dup})")
            continue
        reasons = list(v.reasons)
        if p.params.get("reason") == "agent_failure":              # OR-4: show the operator what actually failed
            reasons.append(f"agent error: {p.params.get('error', 'unknown')}")
        aid = db.execute(
            """INSERT INTO approvals (decision_id, proposal_id, action, agent, entity_ref, summary, proposal, verdict,
               failed_rules, reasons, context, status, reason_code, created_ts, run_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (v.decision_id, p.proposal_id, p.action, p.agent, p.entity_ref, p.summary, p.model_dump_json(),
             v.verdict, db.dumps(v.failed_rules), db.dumps(reasons), db.dumps(p.context),
             "blocked" if v.verdict == "Block" else "pending",
             p.params.get("reason") or (v.failed_rules[0] if v.failed_rules else None), clock.now_iso(), ev.get("run_id")))
        queued.append(aid)
        assigned = _assign_person(p.entity_ref, aid, reasons) if p.action == "resolve" and v.verdict != "Block" else ""
        audit.log("queued", "orchestrator", p.entity_ref, input_summary=p.summary, confidence=p.confidence,
                  reasoning=f"approval #{aid} created ({v.verdict}): " + "; ".join(reasons) + assigned,
                  proposed_action={"action": p.action, "agent": p.agent, "approval_id": aid},
                  verdict=v.verdict, parent_id=v.decision_id, run_id=ev.get("run_id"), event_id=ev.get("event_id"))
        if p.action in SR_ACTIONS and v.verdict != "Block":
            db.update("service_requests", "sr_id", p.entity_ref, {"status": "Pending Approval"})
        trace.append(f"human_queue: approval #{aid} created ({v.verdict}) for '{p.summary}'")
    return {"queued": queued, "trace": trace}


def _assign_person(inc_id: str, approval_id: int, reasons: list[str]) -> str:
    """High risk: the fix waits for a person, so the incident gets a (dummy) on-call engineer who is told about it."""
    inc = db.one("SELECT assigned_team, assigned_agent FROM incidents WHERE incident_id=?", (inc_id,))
    if not inc or (inc["assigned_agent"] or "").startswith("On-call"):
        return ""
    person = f"On-call engineer, {inc['assigned_team'] or 'Service Desk'}"
    db.update("incidents", "incident_id", inc_id, {"assigned_agent": person})
    body = (f"{inc_id} is high risk, so the Guardrail did not let an agent fix it alone ("
            + "; ".join(reasons) + f"). It is assigned to you: approve or reject the fix in approval #{approval_id}.")
    snow.notify("assigned", inc_id, [person], body)
    return f". Assigned {inc_id} to {person} and sent them a notice"


def finalize(state: OpsState) -> dict:
    return {}


def after_finalize(state: OpsState) -> str:
    return "p1_surge" if "p1_surge" in state.get("signals", []) else "__end__"


def p1_surge(state: OpsState) -> dict:
    """OR-2: cross-domain fan-out when a P1 is declared; and the matching all-clear."""
    t0 = time.perf_counter()
    ev = state["event"]
    run_id = ev.get("run_id")
    if ev["type"] == bus.SR_SURGE_OFF:
        db.set_setting("surge_mode", False)
        bus.publish(bus.TRIAGE_RELEASE, ev["entity_ref"], {"reason": "surge cleared"}, run_id=run_id)
        msg = "P1 cleared: surge mode OFF, releasing held reassignments"
        log_decision(state, "orchestrator", ev["entity_ref"], msg, input_summary=ev["type"], confidence=1.0, t0=t0)
        return {"trace": [msg]}
    iid = state.get("entity_ref") or ev["entity_ref"]
    inc = db.one("SELECT * FROM incidents WHERE incident_id=?", (iid,))
    if db.get_setting("surge_mode"):
        msg = f"P1 {iid} joins the active surge (already on)"
        log_decision(state, "orchestrator", iid, msg, input_summary="p1_surge", confidence=1.0, t0=t0)
        return {"trace": [msg]}
    db.set_setting("surge_mode", True)
    system = inc["app_id"] if inc else None
    bus.publish(bus.SR_SURGE_ON, iid, {"system": system}, run_id=run_id)
    bus.publish(bus.KB_VALIDATE_RELATED, iid, {"system": system, "incident_id": iid}, run_id=run_id)
    bus.publish(bus.TRIAGE_HOLD, iid, {"incident_id": iid}, run_id=run_id)
    msg = (f"P1 SURGE for {iid} ({system}): SR agent absorbs the request queue, KB agent validates related articles, "
           "Triage holds non-urgent reassignments")
    log_decision(state, "orchestrator", iid, msg, input_summary="p1_surge", confidence=1.0, t0=t0)
    return {"trace": [msg]}


AGENT_NAMES = ("alert_correlation", "incident_triage", "sr_fulfilment", "kb_curation")

NODE_FUNCS = {
    "route_event": audited("route_event", route_event, "orchestrator"),
    "alert_correlation": audited("alert_correlation", safe("alert_correlation", alert_correlation.run)),
    "incident_triage": audited("incident_triage", safe("incident_triage", incident_triage.run)),
    "sr_fulfilment": audited("sr_fulfilment", safe("sr_fulfilment", sr_fulfilment.run)),
    "kb_curation": audited("kb_curation", safe("kb_curation", kb_curation.run)),
    "guardrail": audited("guardrail", guardrail),
    "execute": audited("execute", execute, "executor"),
    "human_queue": audited("human_queue", human_queue, "orchestrator"),
    "finalize": audited("finalize", finalize, "orchestrator"),
    "p1_surge": audited("p1_surge", p1_surge, "orchestrator"),
}
