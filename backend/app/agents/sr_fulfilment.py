"""SR Fulfilment Agent (SR-1..SR-4, SR-7): deterministic workflows with optional LLM intent parsing."""
from __future__ import annotations

import re
import time

from app import settings
from app.agents.common import log_decision, prop
from app.data import db
from app.graph.state import OpsState
from app.llm import client as llm, prompts
from app.llm.schemas import SRIntent
from app.runtime import bus
from app.tools import kb_store, mock_iam, mock_servicenow as snow

AGENT = "sr_fulfilment"
KNOWN_APPS = list(settings.load_yaml("apps")["apps"])


def knowledge(rtype: str, system: str | None) -> tuple[dict | None, str]:
    """SR-3: the knowledge document whose procedure the fulfilment follows, and a trace line explaining it."""
    th = settings.cfg()["kb"]["accuracy_threshold"]
    doc = kb_store.solution_for(f"{rtype} {system or ''}".strip(), app_id="AuthService" if rtype == "Password Reset" else system)
    if doc is None:
        return None, "Knowledge: no article in the KB covers this request; using the standard workflow"
    acc = f"{doc['score']:.0f}% accuracy" if doc["score"] is not None else "not yet scored"
    line = f"Knowledge: {doc['article_id']} '{doc['title']}' ({acc}"
    line += ")" if doc["usable"] else f", below the {th}% bar - flagged for the KB agent)"
    return doc, line


def parse_text(text: str) -> dict:
    """Rules-based intent parsing for free-text requests."""
    t = (text or "").lower()
    system = next((a for a in KNOWN_APPS if a.lower() in t), None)
    level = "admin" if re.search(r"\b(admin|owner|superuser|root)\b", t) else (
        "read" if re.search(r"\b(read[- ]?only|view)\b", t) else "standard")
    if re.search(r"password|locked out|can'?t (log|sign) ?in|forgot", t):
        rtype = "Password Reset"
    elif re.search(r"access|permission|add me|grant|enrol|enroll", t) and system:
        rtype = "Access Provisioning"
    elif re.search(r"access|permission", t):
        rtype = "Access Provisioning"
    else:
        rtype = "Other"
    return {"request_type": rtype, "target_system": system, "access_level": level if rtype == "Access Provisioning" else None}


def _intent(sr: dict) -> tuple[dict, str, bool]:
    """Returns (intent, mode, llm_parsed). Claude reads every request; structured form fields are kept as given."""
    form = ({"request_type": sr["request_type"], "target_system": sr["target_system"],
             "access_level": sr["access_level"]} if sr["request_type"] else None)
    rules = form or parse_text(sr["text"] or "")
    if llm.is_live():
        res = llm.parse(SRIntent, agent=AGENT, system=prompts.SR_SYSTEM,
                        context="Supported systems: " + ", ".join(KNOWN_APPS),
                        prompt=(f"Employee request: {sr['text'] or '(no free text)'}\n"
                                f"Structured form fields: {form or 'none'}"), effort="low", max_tokens=1024)
        if res is not None and form:
            return form, "llm", True                       # Claude checked the form; it cannot rewrite what the employee picked
        if res is not None:
            return ({"request_type": res.request_type, "target_system": res.target_system,
                     "access_level": res.access_level or rules["access_level"]}, "llm", True)
    return rules, "rules", False


def run(state: OpsState) -> dict:
    if state["event"]["type"] == bus.SR_SURGE_ON:
        return _drain(state)
    return _fulfil(state)


def _drain(state: OpsState) -> dict:
    """SR-7: absorb the human queue while people are pulled onto the P1."""
    t0 = time.perf_counter()
    std = settings.load_yaml("entitlements")["standard_request_types"]
    pending = db.query("SELECT sr_id, run_id FROM service_requests WHERE status='Pending Human' "
                       f"AND request_type IN ({','.join('?' * len(std))}) AND outcome IS NULL", std)
    for r in pending:
        bus.publish(bus.SR_SUBMITTED, r["sr_id"], {"backlog": True}, run_id=r["run_id"])
    msg = f"Surge mode ON: absorbing {len(pending)} pending self-service-eligible request(s) from the human queue"
    log_decision(state, AGENT, state["event"]["entity_ref"], msg, input_summary="sr.surge_mode_on", confidence=1.0, t0=t0)
    return {"trace": [msg]}


def _fulfil(state: OpsState) -> dict:
    t0 = time.perf_counter()
    sr_id = state["event"]["entity_ref"]
    sr = db.one("SELECT * FROM service_requests WHERE sr_id=?", (sr_id,))
    if sr is None:
        raise ValueError(f"unknown service request {sr_id}")
    if sr["status"] == "Completed":
        return {"trace": [f"{sr_id}: already completed"]}
    surge = bool(db.get_setting("surge_mode"))
    intent, mode, _ = _intent(sr)
    db.update("service_requests", "sr_id", sr_id, {"request_type": intent["request_type"],
                                                   "target_system": intent["target_system"],
                                                   "access_level": intent["access_level"],
                                                   "sla_target_hrs": sr["sla_target_hrs"] or snow.sr_sla_target(intent["request_type"])})
    rtype, system, level = intent["request_type"], intent["target_system"], intent["access_level"] or "standard"
    steps = [f"Intent: {rtype}" + (f" on {system} ({level})" if system else "") + f" [{mode}]"]

    # SR-2 identity
    emp = mock_iam.lookup(sr["requestor_id"])
    if emp is None or emp["status"] != "active":
        why = "unknown employee ID" if emp is None else f"employee is {emp['status']}"
        steps.append(f"Identity check FAILED: {why}")
        db.update("service_requests", "sr_id", sr_id, {"status": "Rejected", "outcome": f"identity not verified ({why})"})
        for s in steps:
            snow.append_sr_trace(sr_id, s)
        reasoning = f"{sr_id}: rejected - {why}"
        log_decision(state, AGENT, sr_id, reasoning, input_summary=f"{sr['requestor_id']} {rtype}", confidence=1.0, mode=mode, t0=t0)
        return {"entity_ref": sr_id, "trace": [reasoning]}
    steps.append(f"Identity verified: {emp['name']} ({emp['role']}, {emp['department']})")

    ent = settings.load_yaml("entitlements")
    proposals = []
    conf = 0.98 if mode == "rules" else 0.93
    ctx = {"blast_radius_users": 1, "reversible": True, "app_id": system}
    doc = None
    if rtype in ent["standard_request_types"]:
        doc, line = knowledge(rtype, system)
        steps.append(line)
        if doc:
            ctx.update(kb_article=doc["article_id"], kb_accuracy=doc["score"])
    kb_params = {"kb_article": doc["article_id"], "kb_steps": doc["steps"]} if doc else {}

    def to_human(reason: str) -> None:
        proposals.append(prop(AGENT, "route_to_human", sr_id, f"Route {sr_id} to a human approver: {reason}",
                              params={"reason": reason}, context=ctx))
        steps.append(f"Routed to human: {reason}")

    if rtype == "Password Reset":
        if sr["channel"] == "ticket" and not surge:
            to_human("ticket-channel request; agent pre-validated identity (assist mode, surge mode off)")
        else:
            steps.append("Eligible: standard password reset" + (" (surge mode: absorbed from human queue)" if surge and sr["channel"] == "ticket" else ""))
            proposals.append(prop(AGENT, "reset_password", sr_id, f"Reset password for {sr['requestor_id']}",
                                  confidence=conf, params={"employee_id": sr["requestor_id"], **kb_params},
                                  context={**ctx, "privileged": False, "app_id": "AuthService"}))
    elif rtype == "Access Provisioning":
        privileged = mock_iam.is_privileged(system, level)
        if not system:
            to_human("target system not identified")
        elif privileged:
            steps.append(f"{level} access to {system} is privileged - requires human approval (SR-4)")
            proposals.append(prop(AGENT, "grant_access", sr_id, f"Grant {level} access to {system} for {sr['requestor_id']}",
                                  confidence=conf, params={"employee_id": sr["requestor_id"], "system": system, "level": level, **kb_params},
                                  context={**ctx, "privileged": True}))
        elif not mock_iam.entitled(emp["role"], system):
            to_human(f"role {emp['role']} is not entitled to {system}")
        elif sr["channel"] == "ticket" and not surge:
            to_human("ticket-channel request; agent pre-validated entitlement (assist mode, surge mode off)")
        else:
            steps.append(f"Entitled: role {emp['role']} may self-serve {system}")
            proposals.append(prop(AGENT, "grant_access", sr_id, f"Grant {level} access to {system} for {sr['requestor_id']}",
                                  confidence=conf, params={"employee_id": sr["requestor_id"], "system": system, "level": level, **kb_params},
                                  context={**ctx, "privileged": False}))
    else:
        to_human(f"'{rtype}' is not a standard self-service request")

    for s in steps:
        snow.append_sr_trace(sr_id, s)
    reasoning = f"{sr_id}: " + "; ".join(steps)
    log_decision(state, AGENT, sr_id, reasoning, input_summary=f"{sr['requestor_id']}: {sr['text'] or rtype}",
                 confidence=conf, proposals=proposals, mode=mode, t0=t0)
    return {"entity_ref": sr_id, "proposals": proposals, "trace": [reasoning]}
