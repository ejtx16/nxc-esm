"""Alert Correlation Agent (AC-1..AC-5): group and dedupe alerts, check the KB, hand real problems to Triage."""
from __future__ import annotations

import math
import time

from app import settings
from app.agents.common import app_facts, log_decision, prop
from app.data import audit_ctx, db
from app.graph.state import OpsState
from app.llm import client as llm, prompts
from app.llm.schemas import AlertAssessment
from app.runtime.clock import iso, parse
from app.tools import kb_store, mock_servicenow as snow, runbooks

AGENT = "alert_correlation"
SEV_RANK = {"Low": 0, "Medium": 1, "High": 2, "Critical": 3}
PRIORITIES = ["P1", "P2", "P3", "P4"]


def _noise_ratio(system: str) -> float:
    v = db.scalar("SELECT noise_ratio_pct FROM alert_noise WHERE source_system=?", (system,))
    return (v if v is not None else 35.0) / 100.0


def _name_fp_rate(name: str) -> float:
    rows = db.query("SELECT truth_fp FROM alerts WHERE origin='seed' AND alert_name=?", (name,))
    fp = sum(r["truth_fp"] or 0 for r in rows)
    return (fp + 1) / (len(rows) + 2)


def p_noise(alert: dict) -> tuple[float, dict]:
    """Transparent logistic score from historical evidence (see docs/planning/implementation-plan.md §7.1)."""
    r_sys, r_name = _noise_ratio(alert["source_system"]), _name_fp_rate(alert["alert_name"])
    rec = 1 if alert.get("self_recovered") else 0
    sev_term = {"Critical": -1.2, "High": -0.2, "Medium": 0.4, "Low": 0.8}.get(alert["severity"], 0.0)
    logit = -2.0 + 3.2 * r_sys + 2.0 * r_name + 2.4 * rec + sev_term
    p = 1 / (1 + math.exp(-logit))
    return p, {"system_noise_ratio": round(r_sys, 2), "signature_fp_rate": round(r_name, 2),
               "self_recovered": bool(rec), "severity": alert["severity"]}


def provisional_priority(severity: str, app_id: str) -> str:
    tx = settings.load_yaml("taxonomy")
    base = PRIORITIES.index(tx["severity_priority"].get(severity, "P3"))
    bump = tx["tier_bump"].get(snow.app_tier(app_id), 0)
    return PRIORITIES[max(0, base - bump)]


def _related(a: str, b: str) -> bool:
    for x, y in ((a, b), (b, a)):
        row = db.one("SELECT depends_on FROM apps WHERE app_id=?", (x,))
        if row and y in db.loads(row["depends_on"], []):
            return True
    return False


def find_cluster(alert: dict) -> tuple[dict | None, str]:
    window = settings.cfg()["alert"]["dedupe_window_s"]
    ts = parse(alert["alert_ts"])
    sig = f"{alert['source_system']}|{alert['alert_name']}"
    best, kind = None, ""
    for c in db.query("SELECT * FROM clusters WHERE run_id IS ? ORDER BY first_alert_ts DESC", (alert["run_id"],)):
        if (ts - parse(c["last_alert_ts"])).total_seconds() > window:
            continue
        if c["signature"] == sig:                                           # AC-1 dedupe
            return c, "duplicate"
        systems = db.loads(c["systems"], [])
        incident_grade = c["max_severity"] in ("Critical", "High") and (c["incident_id"] or c["status"] == "acknowledged")
        if incident_grade and best is None and (alert["source_system"] in systems or any(
                _related(alert["source_system"], s) for s in systems)):
            best, kind = c, "correlated"                                    # AC-2 same system / dependency chain
    return best, kind


def _assess(alert: dict, cluster_id: str, p: float, evidence: dict) -> dict:
    cls = "false_positive" if p >= 0.5 else "true_positive"
    conf = p if p >= 0.5 else 1 - p
    out = {"classification": cls, "confidence": round(conf, 3), "mode": "rules",
           "rationale": (f"noise score {p:.2f} (system noise {evidence['system_noise_ratio']:.0%}, signature FP rate "
                         f"{evidence['signature_fp_rate']:.0%}, self-recovered={evidence['self_recovered']})")}
    if llm.is_live():
        ctx = (f"Per-system 6-month noise ratio: {evidence['system_noise_ratio']:.0%}\n"
               f"Historical false-positive rate for this alert name: {evidence['signature_fp_rate']:.0%}\n")
        prompt = (f"Cluster {cluster_id}: alert '{alert['alert_name']}' on {alert['source_system']} "
                  f"(severity {alert['severity']}, self_recovered={evidence['self_recovered']}). "
                  f"Rule-based noise score is {p:.2f}. Classify this cluster.")
        try:
            res = llm.parse(AlertAssessment, agent=AGENT, system=prompts.ALERT_SYSTEM, context=ctx, prompt=prompt,
                            effort="low", max_tokens=1024)
        except llm.LLMTimeout:
            if alert["severity"] != "Critical":
                raise
            res = None                                      # a Critical never waits on Claude: the fast path acks it
        if res is not None:
            out = {"classification": res.classification, "confidence": round(max(0.0, min(1.0, res.confidence)), 3),
                   "mode": "llm", "rationale": res.rationale}
    return out


def _reassess_repeats(alert: dict, cluster_id: str, count: int) -> AlertAssessment | None:
    """Ask Claude again once an unsure group keeps firing; None in rules mode (the repeats alone decide)."""
    prompt = (f"Cluster {cluster_id}: alert '{alert['alert_name']}' on {alert['source_system']} (severity "
              f"{alert['severity']}, Tier {snow.app_tier(alert['source_system'])}) was unsure at first. It has now fired "
              f"{count} times within the dedupe window and none of them cleared on their own. Is it a real problem "
              "that needs an incident?")
    return llm.parse(AlertAssessment, agent=AGENT, system=prompts.ALERT_SYSTEM, context="", prompt=prompt,
                     effort="low", max_tokens=1024)


def _minutes(seconds: float) -> str:
    m = seconds / 60
    return f"{m:g} minute{'s' if m != 1 else ''}"


def _plain_group_reason(alert: dict, cluster: dict | None, kind: str, systems: list[str], count: int) -> str:
    """Plain-English answer to "why did this alert end up in this group?"."""
    window = _minutes(settings.cfg()["alert"]["dedupe_window_s"])
    name, system = alert["alert_name"], alert["source_system"]
    if kind == "duplicate":
        return (f"This is the same alert as the others in {cluster['cluster_id']}: '{name}' on {system}, firing again "
                f"within {window} of the last one. We count it as a repeat instead of raising a new problem, so it is "
                f"alert #{count} in that group.")
    if kind == "correlated":
        others = [s for s in systems if s != system]
        if system in db.loads(cluster["systems"], []):
            link = f"{system} is already affected in that group"
        else:
            rel = next((s for s in others if _related(system, s)), others[0] if others else system)
            link = f"{system} and {rel} depend on each other, so one is likely causing the other"
        return (f"A serious problem ({cluster['max_severity']}) is already being handled in {cluster['cluster_id']}, and "
                f"this alert arrived within {window} of it. {link}, so we group them together instead of "
                f"treating this as a separate issue.")
    return (f"Nothing open in the last {window} matches '{name}' on {system} or is linked to it, so this alert starts "
            f"its own new group.")


def _plain_verdict(assessment: dict, evidence: dict, severity: str) -> str:
    """Plain-English version of the noise score: is this likely real or likely noise, and why."""
    noisy = assessment["classification"] == "false_positive"
    sure = assessment["confidence"]
    level = "very sure" if sure >= 0.85 else "fairly sure" if sure >= 0.7 else "not very sure"
    bits = [f"{evidence['system_noise_ratio']:.0%} of past alerts from this system turned out to be noise",
            f"this kind of alert was a false alarm {evidence['signature_fp_rate']:.0%} of the time"]
    if evidence["self_recovered"]:
        bits.append("it fixed itself")
    verdict = "probably just noise" if noisy else "probably a real problem"
    text = f"We think it is {verdict} ({level}, {sure:.0%}) because " + ", and ".join(bits) + "."
    if severity == "Critical":
        text += " It is Critical, so it is never suppressed, whatever the score says."
    return text


def _track(state: OpsState, alert: dict, cid: str, grouping: str, *, size: int, systems: list[str],
           classification: str, confidence: float, grouping_confidence: float, mode: str, rationale: str,
           decision_id: int | None, explanation: str = "") -> None:
    """One correlation_log row per alert: which group it landed in, why, and how sure the agent was."""
    ev = state.get("event", {})
    db.insert("correlation_log", {
        "ts": iso(parse(alert["alert_ts"])), "run_id": ev.get("run_id"), "decision_id": decision_id,
        "alert_id": alert["alert_id"], "cluster_id": cid, "grouping": grouping,
        "signature": f"{alert['source_system']}|{alert['alert_name']}", "source_system": alert["source_system"],
        "severity": alert["severity"], "cluster_size": size, "cluster_systems": db.dumps(systems),
        "classification": classification, "confidence": round(confidence, 3),
        "grouping_confidence": round(grouping_confidence, 3), "mode": mode, "rationale": rationale,
        "explanation": explanation})


def run(state: OpsState) -> dict:
    t0 = time.perf_counter()
    ev = state["event"]
    alert = db.one("SELECT * FROM alerts WHERE alert_id=?", (ev["entity_ref"],))
    if alert is None:
        raise ValueError(f"unknown alert {ev['entity_ref']}")
    cfg = settings.cfg()["alert"]
    sev = alert["severity"]
    trace: list[str] = []
    proposals = []
    event_real = ev.get("ts_real")

    cluster, kind = find_cluster(alert)
    if cluster:
        cid = cluster["cluster_id"]
        systems = sorted(set(db.loads(cluster["systems"], [])) | {alert["source_system"]})
        max_sev = sev if SEV_RANK[sev] > SEV_RANK[cluster["max_severity"]] else cluster["max_severity"]
        last = max(parse(cluster["last_alert_ts"]), parse(alert["alert_ts"]))
        count = cluster["alert_count"] + 1
        fields = {"alert_count": count, "last_alert_ts": iso(last), "max_severity": max_sev,
                  "systems": db.dumps(systems)}
        if not alert["truth_fp"]:
            fields["truth_tp"] = 1
        reopened = reopened_plain = ""
        mode = "rules"
        if cluster["status"] == "suppressed" and sev != "Critical":
            # New evidence: a suppressed cluster is re-scored by each alert that joins it, so one real alert cannot hide
            # inside a noisy signature. Critical joiners take the escalation path below.
            p_join, _ev = p_noise(alert)
            if p_join < 0.5:
                fields.update(status="surfaced", classification="true_positive", confidence=round(1 - min(p_join, 0.5), 3))
                cluster = {**cluster, "status": "surfaced", "classification": "true_positive",
                           "confidence": fields["confidence"]}
                reopened_plain = (" The group had been marked as noise, but this new alert looks real, so the whole "
                                  "group is back in the work queue.")
                reopened = f"; looks real (noise score {p_join:.2f}), so the cluster is back in the work queue"
        db.update("clusters", "cluster_id", cid, fields)
        status = {"acknowledged": "deduplicated", "suppressed": "suppressed"}.get(cluster["status"], "surfaced")
        if kind == "correlated" and cluster["status"] == "acknowledged":
            status = "correlated"
        db.update("alerts", "alert_id", alert["alert_id"], {
            "cluster_id": cid, "classification": cluster["classification"], "confidence": cluster["confidence"],
            "status": status, "acknowledged": "Yes" if cluster["status"] == "acknowledged" else "No",
            "ack_time_mins": round((cluster["ack_latency_s"] or 0) / 60, 2) if cluster["ack_ts"] else None,
            "incident_id": cluster["incident_id"]})
        reasoning = _plain_group_reason(alert, cluster, kind, systems, count) + reopened_plain
        trace.append(f"{alert['alert_id']}: {reasoning}")
        # A Critical alert joining a cluster that has not been acknowledged yet escalates it (never suppress Critical).
        if sev == "Critical" and not cluster["ack_ts"]:
            p, evidence = p_noise(alert)
            if p < cfg["suppress_confidence"]:
                handoff, kb_note = _ack_and_open(cid, alert, cluster["primary_system"], event_real,
                                                 "Critical alert joined an unacknowledged cluster")
                proposals += handoff
                if cluster["status"] == "suppressed":
                    db.update("clusters", "cluster_id", cid, {"status": "open"})
                reasoning += (" This new alert is Critical and nobody has picked the group up yet, so I am "
                              f"acknowledging it now. {kb_note}")
                trace.append(f"{cid}: Critical alert escalates the cluster - acknowledge + open incident")
        elif (cluster["status"] == "surfaced" and not cluster["ack_ts"] and count == db.get_setting("escalate_repeats")
              and not db.scalar("SELECT COUNT(*) FROM alerts WHERE cluster_id=? AND self_recovered=1", (cid,))):
            # Repeats are evidence (opt-in per scenario): an unsure group that keeps firing may not be a blip.
            view = _reassess_repeats(alert, cid, count)
            if view is None or view.classification == "true_positive":
                handoff, kb_note = _ack_and_open(cid, alert, cluster["primary_system"], event_real,
                                                 f"fired {count} times without clearing", severity=max_sev)
                proposals += handoff
                reasoning += (f" It has now fired {count} times and never cleared on its own, so it is not a one-off "
                              f"blip." + (f" Claude agrees: {view.rationale}" if view else "")
                              + f" I am acknowledging {cid}. {kb_note}")
                trace.append(f"{cid}: repeated {count} times - acknowledge + open incident")
            else:
                reasoning += (f" It has now fired {count} times, but Claude still thinks it is noise, so it keeps "
                              f"waiting for a person: {view.rationale}")
            mode = "llm" if view else mode
        elif (cluster["status"] == "surfaced" and not cluster["ack_ts"] and cluster["classification"] == "false_positive"
              and sev != "Critical" and alert["self_recovered"]
              and not db.scalar("SELECT COUNT(*) FROM alerts WHERE cluster_id=? AND self_recovered=0", (cid,))):
            # Repeats are evidence the other way too: each repeat that clears on its own halves the remaining doubt.
            conf = round(1 - (1 - cluster["confidence"]) / 2, 3)
            db.update("clusters", "cluster_id", cid, {"confidence": conf})
            if conf >= cfg["suppress_confidence"]:
                proposals.append(prop(AGENT, "suppress", cid,
                                      f"Suppress {cid}: {alert['alert_name']} on {alert['source_system']} keeps "
                                      "clearing on its own", confidence=conf,
                                      context={"severity": sev, **app_facts(alert["source_system"])}))
                reasoning += (f" It cleared on its own again, like every alert in the group, so I am now {conf:.0%} sure "
                              f"it is noise and am asking the Guardrail to suppress {cid}.")
            else:
                reasoning += f" It cleared on its own again, so I am now {conf:.0%} sure it is noise - not sure enough yet."
        group_conf = 1.0 if kind == "duplicate" else 0.9
        did = log_decision(state, AGENT, cid, reasoning, input_summary=f"{alert['alert_id']} {alert['alert_name']}",
                           confidence=group_conf, proposals=proposals, mode=mode, t0=t0)
        _track(state, alert, cid, kind, size=count, systems=systems,
               classification=cluster["classification"], confidence=cluster["confidence"],
               grouping_confidence=group_conf, mode=cluster["mode"] or "rules", rationale=reasoning, decision_id=did,
               explanation=_plain_group_reason(alert, cluster, kind, systems, count) + reopened_plain)
        return {"entity_ref": cid, "proposals": proposals, "trace": trace}

    # --- new cluster ---------------------------------------------------------------------------
    cid = snow.next_cluster_id()
    p, evidence = p_noise(alert)
    assessment = _assess(alert, cid, p, evidence)
    db.insert("clusters", {
        "cluster_id": cid, "run_id": alert["run_id"], "primary_system": alert["source_system"],
        "signature": f"{alert['source_system']}|{alert['alert_name']}", "systems": db.dumps([alert["source_system"]]),
        "first_alert_ts": alert["alert_ts"], "last_alert_ts": alert["alert_ts"], "alert_count": 1,
        "max_severity": sev, "status": "open", "classification": assessment["classification"],
        "confidence": assessment["confidence"], "rationale": assessment["rationale"], "incident_id": None,
        "truth_tp": 0 if alert["truth_fp"] else 1, "created_real": event_real or time.perf_counter(),
        "mode": assessment["mode"]})
    db.update("alerts", "alert_id", alert["alert_id"], {
        "cluster_id": cid, "classification": assessment["classification"], "confidence": assessment["confidence"],
        "status": "surfaced"})

    seen = (f"{_plain_group_reason(alert, None, 'new', [], 1)} {_plain_verdict(assessment, evidence, sev)}"
            + (f" Claude's view: {assessment['rationale']}" if assessment["mode"] == "llm" else ""))
    if sev == "Critical" and p < cfg["suppress_confidence"]:
        # AC-5 fast path: whatever Claude said, a Critical is never suppressed; acknowledge + open an incident now.
        handoff, kb_note = _ack_and_open(cid, alert, alert["source_system"], event_real,
                                         f"Critical {alert['source_system']} alert, noise score {p:.2f}")
        proposals += handoff
        reasoning = (f"{seen} {alert['source_system']} is a Tier {snow.app_tier(alert['source_system'])} system and "
                     f"this is Critical, so I am acknowledging it straight away. {kb_note}")
    elif (assessment["classification"] == "false_positive" and sev != "Critical"
          and assessment["confidence"] >= cfg["suppress_confidence"]):
        proposals.append(prop(AGENT, "suppress", cid,
                              f"Suppress {cid}: {alert['alert_name']} on {alert['source_system']} looks like noise",
                              confidence=assessment["confidence"],
                              context={"severity": sev, **app_facts(alert["source_system"])}))
        reasoning = f"{seen} I am asking the Guardrail to suppress {cid} so nobody is paged for noise."
    elif (assessment["classification"] == "true_positive" and sev != "Critical"
          and assessment["confidence"] >= cfg["suppress_confidence"]):      # same bar as suppressing noise
        handoff, kb_note = _ack_and_open(cid, alert, alert["source_system"], event_real,
                                         f"{sev} {alert['source_system']} alert looks real", severity=sev)
        proposals += handoff
        reasoning = f"{seen} {kb_note}"
    else:
        db.update("clusters", "cluster_id", cid, {"status": "surfaced"})
        reasoning = (f"{seen} I am not sure enough either way, so {cid} waits in the queue for a person to look at "
                     "instead of me opening an incident or hiding it.")
    trace.append(reasoning)
    did = log_decision(state, AGENT, cid, reasoning, input_summary=f"{alert['alert_id']} {alert['alert_name']} "
                       f"[{alert['source_system']}/{sev}]", confidence=assessment["confidence"], proposals=proposals,
                       mode=assessment["mode"], t0=t0)
    _track(state, alert, cid, "new_cluster", size=1, systems=[alert["source_system"]],
           classification=assessment["classification"], confidence=assessment["confidence"],
           grouping_confidence=1.0, mode=assessment["mode"], rationale=reasoning, decision_id=did,
           explanation=reasoning)
    if alert["truth_fp"] is not None:      # scenario/seed ground truth -> online accuracy (AU-4)
        audit_ctx.record_accuracy(AGENT, "alert_classification", predicted=assessment["classification"],
                                  expected="false_positive" if alert["truth_fp"] else "true_positive",
                                  source="true_positive", confidence=assessment["confidence"], decision_id=did,
                                  mode=assessment["mode"])
    return {"entity_ref": cid, "proposals": proposals, "trace": trace}


def _kb_check(alert: dict, system: str) -> tuple[list[str], str]:
    """Look for a trusted KB article before handing off; returns (article ids, plain-English note)."""
    rb = runbooks.match(system, f"{system} {alert['alert_name']}")
    art = kb_store.get(rb["kb_article"]) if rb else None
    if art and kb_store.usable(art):
        return [art["article_id"]], (f"I checked the knowledge base and found a trusted fix: runbook {rb['id']} "
                                     f"'{rb['name']}', backed by {art['article_id']} '{art['title']}' "
                                     f"(accuracy {art['score']:.0f}%).")
    good, stale = kb_store.search(f"{system} {alert['alert_name']}", k=1, app_id=system)
    th = settings.cfg()["triage"]["kb_attach_min_accuracy"]
    if good:
        a = good[0]
        return [a["article_id"]], (f"I checked the knowledge base and found a trusted fix: {a['article_id']} "
                                   f"'{a['title']}' (accuracy {a['score']:.0f}%).")
    if stale:
        a = stale[0]
        score = f"{a['score']:.0f}%" if a["score"] is not None else "not scored yet"
        return [], (f"I checked the knowledge base. The closest article, {a['article_id']} '{a['title']}', is "
                    f"{score} accurate, below the {th}% we trust, so I am not passing it on.")
    return [], "I checked the knowledge base and found no article that covers this."


def _ack_and_open(cid: str, alert: dict, system: str, event_real, why: str,
                  severity: str = "Critical") -> tuple[list, str]:
    """Acknowledge the group and hand it to Incident Triage with what the KB check found."""
    facts = app_facts(system)
    prio = provisional_priority(severity, system)
    kb_ids, kb_note = _kb_check(alert, system)
    note = (f"{kb_note} Handing {cid} to the Incident Triage agent as a new {prio} incident"
            + (f" with {kb_ids[0]} attached, so it can use that fix to resolve it." if kb_ids else
               " so it can work out the fix."))
    return [
        prop(AGENT, "acknowledge", cid, f"Acknowledge {cid}", confidence=0.95,
             params={"event_real": event_real}, context=facts),
        prop(AGENT, "open_incident", cid, f"Open {prio} incident for {cid} ({system}: {alert['alert_name']})",
             confidence=0.95, params={"provisional_priority": prio, "origin": "scenario", "kb_article_ids": kb_ids,
                                      "description": f"{system}: {alert['alert_name']}", "why": why},
             context={"priority": prio, **facts}),
    ], note
