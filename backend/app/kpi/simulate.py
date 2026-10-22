"""KPI simulation (DB-3): replays the scenarios in an ISOLATED database (subprocess) and labels each KPI measured/modelled."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from app import settings
from app.kpi import baseline as base

_status: dict = {"running": False, "started": None, "error": None, "finished": None}


# --- helpers (run inside the isolated process) -----------------------------------------------------------
def _pct(n: float, d: float) -> float:
    return round(100.0 * n / d, 1) if d else 0.0


def _fresh() -> None:
    from app.data import db, seed
    from app.runtime import engine
    db.configure()
    seed.run(reset=True)
    engine.reset_state()


def _alert_metrics(run_id: str) -> dict:
    from app.data import db
    cl = db.query("SELECT * FROM clusters WHERE run_id=?", (run_id,))
    raw = db.scalar("SELECT COUNT(*) FROM alerts WHERE run_id=?", (run_id,), default=0)
    fp_raw = db.scalar("SELECT SUM(truth_fp) FROM alerts WHERE run_id=?", (run_id,), default=0)
    surfaced = [c for c in cl if c["status"] != "suppressed"]
    fp_surf = [c for c in surfaced if not c["truth_tp"]]
    fp_open = sum(1 for c in fp_surf if not db.scalar("SELECT MAX(self_recovered) FROM alerts WHERE cluster_id=?",
                                                       (c["cluster_id"],), default=0))
    tp_suppressed = [c for c in cl if c["status"] == "suppressed" and c["truth_tp"]]
    crit_acks = [c for c in cl if c["ack_ts"]]
    return {"raw_alerts": raw, "raw_fp": fp_raw, "raw_noise_pct": _pct(fp_raw, raw), "clusters": len(cl),
            "surfaced": len(surfaced), "surfaced_fp": len(fp_surf), "surfaced_fp_not_recovered": fp_open, "suppressed": len(cl) - len(surfaced),
            "tp_wrongly_suppressed": len(tp_suppressed), "acks_real_ms": [c["ack_real_ms"] for c in crit_acks],
            "acks_sim_s": [c["ack_latency_s"] for c in crit_acks]}


def _triage_ms() -> list[float]:
    from app.data import db
    return [r["triage_ms"] for r in db.query("SELECT triage_ms FROM incidents WHERE origin!='seed' AND triage_ms IS NOT NULL")]


def _sr_metrics() -> dict:
    from app.data import db
    from app.runtime.clock import clock, parse
    end = clock.now()
    rows = db.query("SELECT * FROM service_requests WHERE origin!='seed'")
    done = [r for r in rows if r["status"] == "Completed"]
    auto = [r for r in done if (r["fulfilled_by"] or "").startswith("agent:")]
    breached = 0
    for r in rows:
        target = r["sla_target_hrs"] or 24
        if r["status"] == "Completed":
            breached += 1 if r["sla_met"] == "No" else 0
        elif r["status"] != "Rejected":
            breached += 1 if (end - parse(r["submitted_ts"])).total_seconds() / 3600 > target else 0
    by = {}
    for r in rows:
        by[r["status"]] = by.get(r["status"], 0) + 1
    return {"total": len(rows), "auto": len(auto), "completed": len(done), "breached": breached, "by_status": by,
            "self_service_pct": _pct(len(auto), len(rows)), "breach_pct": _pct(breached, len(rows)),
            "pending_approval": by.get("Pending Approval", 0), "pending_human": by.get("Pending Human", 0)}


def _kb_model() -> dict:
    from app.agents import kb_curation
    from app.data import db
    from app.runtime import approvals, bus, engine
    th = settings.cfg()["kb"]["accuracy_threshold"]
    f = settings.cfg()["kb"]["post_update_escalation_factor"]
    before = db.query("SELECT * FROM kb_articles")
    mean_before_all = sum(a["score"] for a in before) / len(before)
    active_before = [a for a in before if a["recommendation"] != "archive"]
    for a in before:
        if a["recommendation"] == "update":
            bus.publish(bus.KB_REVIEW, a["article_id"], {"draft": True})
    engine.process_pending()
    for ap in db.query("SELECT approval_id FROM approvals WHERE action='update_kb_article' AND status='pending'"):
        approvals.decide(ap["approval_id"], True, "kb.manager (modelled)")
    after = db.query("SELECT * FROM kb_articles")
    active = [a for a in after if a["recommendation"] != "archive"]
    mean_after = sum(a["score"] for a in active) / len(active)
    usable = [a for a in after if a["score"] >= th and a["recommendation"] != "archive"]
    esc_before = sum(a["escalation_despite_kb"] for a in before) / max(1, sum(a["usage_count_90days"] for a in before))
    esc_after = (sum(a["escalation_despite_kb"] * (f if a["updated_by_agent"] else 1.0) for a in usable)
                 / max(1, sum(a["usage_count_90days"] for a in usable)))
    return {"articles": len(after), "mean_before_all": round(mean_before_all, 1),
            "mean_before_active": round(sum(a["score"] for a in active_before) / len(active_before), 1),
            "mean_after_active": round(mean_after, 1), "active_after": len(active), "archived": len(after) - len(active),
            "updated": sum(1 for a in after if a["updated_by_agent"]), "usable_after": len(usable),
            "below_after": sum(1 for a in active if a["score"] < th),
            "esc_ratio_before_pct": round(100 * esc_before, 1), "esc_ratio_after_pct": round(100 * esc_after, 1),
            "usable_before": sum(1 for a in before if a["score"] >= th and a["recommendation"] != "archive")}


def _mttr_rows() -> list[dict]:
    """The P1/P2 incidents of the (dummy) incident log, each flagged with whether triage matches a runbook."""
    from app.agents import incident_triage
    from app.data import db
    rows = db.query("SELECT * FROM incidents WHERE origin='seed' AND priority IN ('P1','P2') AND resolution_time_hrs IS NOT NULL")
    return [{**r, "runbook": bool(incident_triage.decide(r["short_description"], app_id=r["app_id"],
                                                         allow_llm=False)["runbook"])} for r in rows]


def _mttr_model(assumptions: dict | None = None, runbook_success_pct: float = 100.0,
                rows: list[dict] | None = None) -> dict:
    """Apply the stated levers to the 22 P1/P2 incidents in the (dummy) incident log.

    `runbook_success_pct` < 100 is a what-if (validation V3): a failed runbook fix falls back to the manual path
    after the approval and fix time are spent. Times and compliance are then expected values."""
    A = {**base.ASSUMPTIONS, **(assumptions or {})}
    sla = settings.cfg()["sla_hours"]
    rows = rows if rows is not None else _mttr_rows()
    s = runbook_success_pct / 100
    new_times, matched = [], 0
    for r in rows:
        saved = A["triage_delay_saved_h"] + (A["ack_lag_saved_h"] if r["priority"] == "P1" else 0)
        if r["sla_breach"] == "Yes":
            saved += A["escalation_lag_saved_h"]
        manual = max(0.25, (r["resolution_time_hrs"] - saved) * (1 - A["first_time_right_routing_pct"] / 100))
        if r["runbook"]:
            matched += 1
            fix = A["approval_latency_h"] + A["runbook_fix_h"]
            outcomes = [(s, fix), (1 - s, fix + manual)]
        else:
            outcomes = [(1.0, manual)]
        mean = sum(p * t for p, t in outcomes)
        met = sum(p for p, t in outcomes if t <= sla[r["priority"]])
        new_times.append((r, mean, met))
    n = len(rows)
    before_mean = sum(r["resolution_time_hrs"] for r in rows) / n
    after_mean = sum(t for _, t, _ in new_times) / n
    comp_before = sum(1 for r in rows if r["resolution_time_hrs"] <= sla[r["priority"]])
    comp_after = sum(m for _, _, m in new_times)
    return {"incidents": n, "runbook_matched": matched, "mean_before_h": round(before_mean, 1),
            "mean_after_h": round(after_mean, 1), "compliance_before_pct": _pct(comp_before, n),
            "compliance_after_pct": _pct(comp_after, n)}


def _case_scaled(mt: dict) -> dict:
    """Carry the incident-log change over to the case baselines (48 h MTTR, 33% SLA compliance, $31M)."""
    gap_closed = (mt["compliance_after_pct"] - mt["compliance_before_pct"]) / max(1.0, 100 - mt["compliance_before_pct"])
    sla_after = 33 + (100 - 33) * max(0.0, gap_closed)          # the share of the non-compliant gap the levers close
    return {"mttr": 48 * mt["mean_after_h"] / mt["mean_before_h"], "sla_compliance": sla_after,
            "cost": 31 * (100 - sla_after) / (100 - 33)}


def _evals(allow_llm: bool = False, record: bool = False) -> dict:
    """Batch accuracy on the golden set and seed alerts. `record` also writes `scope='batch'` audit rows (AU-4)."""
    import csv
    from app.agents import alert_correlation, incident_triage
    from app.data import audit_ctx, db
    out: dict = {}
    rows = list(csv.DictReader(open(settings.GOLDEN_PATH)))
    cat_ok = pri_ok = pri_exact = 0
    miss = []
    modes: set[str] = set()
    for r in rows:
        d = incident_triage.decide(r["description"], allow_llm=allow_llm)
        modes.add(d["mode"])
        cat_ok += d["category"] == r["category"]
        pri_exact += d["priority"] == r["priority"]
        within = abs(int(d["priority"][1]) - int(r["priority"][1])) <= 1
        pri_ok += within
        if record:
            audit_ctx.record_accuracy("incident_triage", "triage_category", predicted=d["category"],
                                      expected=r["category"], source="batch_eval", scope="batch",
                                      confidence=d["confidence"], mode=d["mode"])
            audit_ctx.record_accuracy("incident_triage", "triage_priority_within_1", predicted=d["priority"],
                                      expected=r["priority"], correct=within, source="batch_eval", scope="batch",
                                      confidence=d["confidence"], mode=d["mode"])
        if d["category"] != r["category"] or not within:
            miss.append({"description": r["description"], "expected": f"{r['category']}/{r['priority']}",
                         "got": f"{d['category']}/{d['priority']}"})
    out["triage"] = {"n": len(rows), "category_acc_pct": _pct(cat_ok, len(rows)), "priority_within_1_pct": _pct(pri_ok, len(rows)),
                     "priority_exact_pct": _pct(pri_exact, len(rows)), "target": "category >= 80%, priority within +/-1 >= 90%",
                     "misses": miss, "modes": sorted(modes)}
    alerts = db.query("SELECT * FROM alerts WHERE origin='seed'")
    ok = tp = 0
    for a in alerts:
        p, _ = alert_correlation.p_noise({**a, "self_recovered": a["self_recovered"]})
        pred_fp = p >= 0.5
        ok += pred_fp == bool(a["truth_fp"])
    out["alert_classification"] = {"n": len(alerts), "accuracy_pct": _pct(ok, len(alerts)),
                                   "note": "rules classifier vs True_Positive on the 80-row seed alert log (reported, not gating)"}
    return out


def run_simulation() -> dict:
    from app.data import db
    from app.runtime import player
    from app.llm import client as llm
    db.configure()
    t0 = time.time()
    llm.set_client(None)
    result: dict = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"), "llm_mode": "rules", "scenarios": {}}

    _fresh()
    rid = player.run_sync("licensecore_outage")
    outage = _alert_metrics(rid)
    outage_tri = _triage_ms()
    guard = {r["verdict"] + ":" + r["action"]: r["n"] for r in db.query(
        "SELECT verdict, action, COUNT(*) n FROM approvals GROUP BY verdict, action")}
    p1 = db.one("SELECT * FROM incidents WHERE priority='P1' AND origin!='seed' ORDER BY incident_id LIMIT 1")
    resolve_ap = db.one("SELECT failed_rules, verdict FROM approvals WHERE action='resolve' AND entity_ref=?", (p1["incident_id"],)) if p1 else None
    unattended_before = db.scalar("SELECT COUNT(*) FROM incidents WHERE origin!='seed' AND status='Resolved'", default=0)
    # A knowledge manager approves the KB refreshes the Triage Agent asked for (modelled); the safe incident then self-heals.
    from datetime import datetime
    from app.runtime import approvals, engine
    from app.runtime.clock import clock
    clock.jump_to(datetime(2025, 8, 6, 9, 0))        # the 28th-5th billing freeze has lifted; inside it nothing self-heals
    for ap in db.query("SELECT approval_id FROM approvals WHERE action='update_kb_article' AND status='pending'"):
        approvals.decide(ap["approval_id"], True, "kb.manager (modelled)")
    while engine.process_pending():
        pass
    healed = [r["incident_id"] for r in db.query("SELECT incident_id FROM incidents WHERE origin!='seed' AND status='Resolved'")]
    auto_resolved = len(healed) - unattended_before
    cl1 = db.one("SELECT * FROM clusters WHERE incident_id=?", (p1["incident_id"],)) if p1 else None
    result["scenarios"]["licensecore_outage"] = {
        **{k: v for k, v in outage.items() if not k.endswith("_ms") and not k.endswith("_s")},
        "p1_incident": p1["incident_id"] if p1 else None, "p1_cluster_alerts": cl1["alert_count"] if cl1 else None,
        "p1_priority": p1["priority"] if p1 else None, "p1_team": p1["assigned_team"] if p1 else None,
        "p1_ack_real_ms": cl1["ack_real_ms"] if cl1 else None, "p1_ack_sim_s": cl1["ack_latency_s"] if cl1 else None,
        "guardrail_resolve_verdict": resolve_ap["verdict"] if resolve_ap else None,
        "guardrail_failed_rules": db.loads(resolve_ap["failed_rules"], []) if resolve_ap else [],
        "approvals_by_verdict_action": guard, "auto_resolved_unattended": auto_resolved, "auto_resolved_ids": healed,
        "surge_mode_triggered": bool(db.get_setting("surge_mode")),
        "sr": _sr_metrics()}

    _fresh()
    rid = player.run_sync("normal_day")
    day = _alert_metrics(rid)
    day_tri = _triage_ms()
    result["scenarios"]["normal_day"] = {**{k: v for k, v in day.items() if not k.endswith("_ms") and not k.endswith("_s")},
                                         "triaged": len(day_tri), "sr": _sr_metrics()}

    _fresh()
    player.run_sync("sr_surge")
    sr = _sr_metrics()
    result["scenarios"]["sr_surge"] = sr

    _fresh()
    kb = _kb_model()
    mt = _mttr_model()
    result["evals"] = _evals()

    surf = outage["surfaced"] + day["surfaced"]
    surf_fp = outage["surfaced_fp"] + day["surfaced_fp"]
    raw = outage["raw_alerts"] + day["raw_alerts"]
    raw_fp = outage["raw_fp"] + day["raw_fp"]
    acks = outage["acks_real_ms"] + day["acks_real_ms"]
    tri = outage_tri + day_tri
    A = base.ASSUMPTIONS
    scaled = _case_scaled(mt)
    sla_after, cost_after, mttr_after = scaled["sla_compliance"], scaled["cost"], scaled["mttr"]
    kb_esc_after = 64 * (kb["esc_ratio_after_pct"] / kb["esc_ratio_before_pct"])

    def entry(key, after, kind, method, detail, display=None):
        b = base.BY_KEY[key]
        better = after <= b["target"] if b["better"] == "lower" else after >= b["target"]
        return {**b, "after": round(after, 3) if after < 1 else round(after, 1), "kind": kind, "method": method,
                "detail": detail, "target_met": bool(better), "after_display": display}

    ack_ms = sum(acks) / len(acks) if acks else None
    tri_ms = sum(tri) / len(tri) if tri else None
    result["kpis"] = [
        entry("alert_noise", _pct(surf_fp, surf), "measured",
              "False-positive clusters still surfaced / all surfaced clusters, licensecore_outage + normal_day (ground truth from the scenario generator).",
              {"raw_alerts": raw, "raw_noise_pct": _pct(raw_fp, raw), "clusters": outage["clusters"] + day["clusters"],
               "surfaced": surf, "surfaced_fp": surf_fp,
               "surfaced_fp_not_recovered": outage["surfaced_fp_not_recovered"] + day["surfaced_fp_not_recovered"], "suppressed": outage["suppressed"] + day["suppressed"],
               "true_positives_wrongly_suppressed": outage["tp_wrongly_suppressed"] + day["tp_wrongly_suppressed"]}),
        entry("ack_time", (ack_ms or 0) / 60000, "measured",
              "Real processing latency from alert.received to acknowledgement for auto-acknowledged clusters (prototype path only; excludes production integration latency)",
              {"clusters_acked": len(acks), "mean_ms": round(ack_ms or 0, 1),
               "outage_sim_s": outage["acks_sim_s"][:1]}, display=f"{(ack_ms or 0) / 1000:.2f} s"),
        entry("triage_time", (tri_ms or 0) / 60000, "measured",
              "Real latency from incident.created to triage decision (rules mode; add ~5-15 s per incident in live mode)",
              {"incidents_triaged": len(tri), "mean_ms": round(tri_ms or 0, 1)}, display=f"{(tri_ms or 0) / 1000:.2f} s"),
        entry("self_service", sr["self_service_pct"], "measured",
              "SRs fulfilled by the agent / all SRs in the 3,200-request sr_surge scenario",
              {"total": sr["total"], "auto": sr["auto"], "by_status": sr["by_status"]}),
        entry("sr_breach", sr["breach_pct"], "measured",
              "Completed-after-target + still-open-past-target SRs / all SRs at the end of sr_surge",
              {"total": sr["total"], "breached": sr["breached"], "pending_human": sr["pending_human"],
               "pending_approval": sr["pending_approval"]}),
        entry("kb_accuracy", kb["mean_after_active"], "modelled",
              "Mean recomputed score of the maintained corpus after every drafted update is approved (approval assumed)",
              {k: kb[k] for k in ("mean_before_all", "mean_before_active", "mean_after_active", "updated", "archived", "below_after")}),
        entry("kb_escalation", kb_esc_after, "modelled",
              "Case baseline scaled by the change in escalation ratio once triage only recommends articles above the threshold and updated articles escalate less",
              {"ratio_before_pct": kb["esc_ratio_before_pct"], "ratio_after_pct": kb["esc_ratio_after_pct"],
               "usable_before": kb["usable_before"], "usable_after": kb["usable_after"]}),
        entry("mttr", mttr_after, "modelled",
              "Case baseline scaled by the change in mean resolution time of the 22 P1/P2 incidents in the incident log after applying the assumptions below",
              {**mt, "assumptions": {k: v for k, v in A.items() if k != "kb_updates_approved"}}),
        entry("sla_compliance", sla_after, "modelled",
              "Case baseline closed by the same share of its non-compliant gap as the incident log closes under the same assumptions (never above 100%)",
              {"log_before_pct": mt["compliance_before_pct"], "log_after_pct": mt["compliance_after_pct"], "n": mt["incidents"]}),
        entry("cost", cost_after, "modelled",
              "$31M x (non-compliance after / non-compliance before) using the modelled SLA compliance",
              {"sla_after_pct": round(sla_after, 1)}),
    ]
    result["assumptions"] = A
    result["duration_s"] = round(time.time() - t0, 1)
    return result


# --- API-process side ------------------------------------------------------------------------------------
def _run_subprocess() -> None:
    from app.data import db
    out = settings.DATA_DIR / "kpi_results.json"
    env = {**os.environ, "ESM_DB_PATH": str(settings.DATA_DIR / "kpi_sim.db"), "LLM_MODE": "rules",
           "ESM_SIM_START": settings.CASE_START.isoformat()}   # modelled against the case calendar, not today
    _status.update(running=True, started=time.time(), error=None)
    try:
        proc = subprocess.run([sys.executable, "-m", "app.kpi.simulate", "--out", str(out)], cwd=settings.BASE_DIR,
                              env=env, capture_output=True, text=True, timeout=900)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr[-1500:] or proc.stdout[-500:])
        payload = json.loads(out.read_text())
        db.execute("INSERT INTO kpi_results (created, payload) VALUES (?,?)", (payload["generated"], json.dumps(payload)))
        _status.update(finished=time.time())
    except Exception as exc:  # noqa: BLE001
        _status.update(error=f"{type(exc).__name__}: {exc}")
    finally:
        _status["running"] = False


def start_background() -> dict:
    if not _status["running"]:
        threading.Thread(target=_run_subprocess, daemon=True, name="kpi-sim").start()
        _status["running"] = True
    return {"started": True, **_status}


def latest() -> dict:
    from app.data import db
    row = db.one("SELECT payload FROM kpi_results ORDER BY id DESC LIMIT 1")
    return {"baseline": base.KPIS, "results": json.loads(row["payload"]) if row else None, "status": dict(_status),
            "assumptions": base.ASSUMPTIONS}


def ensure_results() -> None:
    from app.data import db
    if db.scalar("SELECT COUNT(*) FROM kpi_results", default=0) == 0:
        start_background()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--eval-llm", action="store_true",
                    help="only run the golden-set evaluation with live Claude and record batch accuracy rows")
    args = ap.parse_args()
    if args.eval_llm:
        from app.data import db
        from app.llm import client as llm
        os.environ["LLM_MODE"] = "live"
        db.configure()
        if llm.mode() != "live":
            print("warning: the Controls LLM-mode setting is 'rules' in this database; evaluating the rules path")
        res = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"), "evals": _evals(allow_llm=True, record=True)}
        Path(args.out).write_text(json.dumps(res, indent=2))
        print(f"golden-set evaluation ({res['evals']['triage']['modes']}): {res['evals']['triage']}")
        return
    res = run_simulation()
    Path(args.out).write_text(json.dumps(res, indent=2))
    print(f"KPI simulation finished in {res['duration_s']}s")


if __name__ == "__main__":
    main()
