"""Validation scenarios (docs/validation/validation-scenarios.md): three runs that test whether the KPI results hold up.

V1 excel_calibrated   the alert agent on a day drawn from the Excel distributions vs the case-calibrated normal_day
V2 human_baseline     the same P1 + SR surge event streams handled by the AS-IS human process vs the agents (the cascade)
V3 sensitivity        the modelled MTTR / SLA / cost KPIs under pessimistic assumptions, with break-even points

    .venv/bin/python -m app.kpi.validation --out data/validation_results.json

Runs in rules mode in its own database (data/validation.db) on the case calendar, like the KPI simulation.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

from app import settings
from app.kpi import baseline as base
from app.kpi.simulate import _pct

# AS-IS human process (V2). Sources: the case study (docs/product/business-problem.md), the Excel files, or labelled assumptions.
AS_IS = {
    "sr_team_size": 4,                     # case: 4-person SR team
    "sr_team_throughput_per_week": 700,    # case: handles 700+ SRs/week
    "sr_team_hours_per_week": 40,          # assumed: one working week per person
    "manual_ack_min": 27.0,                # Excel: mean Ack_Time_Mins of the Alert Log (case figure is 22)
    "manual_triage_min": 18.0,             # case: 18 min per incident
    "alert_review_min": 5.0,               # assumed: L1 minutes to read and dismiss or act on one alert
    "approval_min": 15.0,                  # assumed, = ASSUMPTIONS approval_latency_h: a person approves a held action
    "case_sr_breach_pct": 84.0,            # case: SR SLA breach during the backlog (calibration target)
}

# V3 sweep values; the first is the default used by the KPI simulation.
SWEEP = {
    "runbook_success_pct": [100.0, 90.0, 75.0, 50.0, 25.0, 0.0],
    "approval_latency_h": [0.25, 0.5, 1.0, 2.0, 4.0],
    "first_time_right_routing_pct": [25.0, 0.0, 50.0],
}


# --- V1: Excel-calibrated alert day -----------------------------------------------------------------------
def _noise_quality(run_id: str) -> dict:
    """Suppression graded at cluster level against the generator's ground truth."""
    from app.data import db
    from app.kpi.simulate import _alert_metrics
    m = _alert_metrics(run_id)
    cl = db.query("SELECT status, truth_tp FROM clusters WHERE run_id=?", (run_id,))
    supp = [c for c in cl if c["status"] == "suppressed"]
    noise = [c for c in cl if not c["truth_tp"]]
    caught = sum(1 for c in supp if not c["truth_tp"])
    return {"raw_alerts": m["raw_alerts"], "raw_noise_pct": m["raw_noise_pct"], "clusters": m["clusters"],
            "noise_if_nothing_suppressed_pct": _pct(len(noise), len(cl)),
            "noise_after_pct": _pct(m["surfaced_fp"], m["surfaced"]), "suppressed": len(supp),
            "suppression_precision_pct": _pct(caught, len(supp)), "suppression_recall_pct": _pct(caught, len(noise)),
            "true_positives_wrongly_suppressed": m["tp_wrongly_suppressed"]}


def _profile_summary(prof: dict) -> dict:
    out = {}
    for label in ("fp", "tp"):
        p = prof[label]
        out[label] = {"n": p["n"], "p_recovered_pct": round(100 * p["p_recovered"], 1),
                      "severity_pct": {s: _pct(v, p["n"]) for s, v in p["severity"].items()}}
    crit = prof["fp"]["severity"]["Critical"] + prof["tp"]["severity"]["Critical"]
    out["critical_that_are_fp_pct"] = _pct(prof["fp"]["severity"]["Critical"], crit)
    return out


def v1_excel_calibrated() -> dict:
    from app.data import scenarios
    from app.kpi.simulate import _fresh, _sr_metrics
    from app.runtime import player
    arms = {}
    for name in ("normal_day", "excel_calibrated_day"):
        _fresh()
        prof = scenarios.excel_profile() if name == "excel_calibrated_day" else None
        rid = player.run_sync(name)
        arms[name] = {**_noise_quality(rid), "sr_self_service_pct": _sr_metrics()["self_service_pct"]}
        if prof:
            arms[name]["excel_profile"] = _profile_summary(prof)
    a, b = arms["normal_day"], arms["excel_calibrated_day"]
    return {"arms": arms, "target_noise_pct": base.BY_KEY["alert_noise"]["target"],
            "delta_noise_after_pts": round(b["noise_after_pct"] - a["noise_after_pct"], 1),
            "noise_reduction_pts": {n: round(x["noise_if_nothing_suppressed_pct"] - x["noise_after_pct"], 1)
                                    for n, x in arms.items()},
            "reading": ("The alert agent's noise reduction holds on Excel-shaped data"
                        if b["noise_after_pct"] <= a["noise_after_pct"] + 5 and b["noise_if_nothing_suppressed_pct"] - b["noise_after_pct"] >= 5
                        else "The alert agent's noise reduction depends on signals the Excel data does not show "
                             "(self-recovery and severity that separate noise from real alerts)")}


# --- V2: human-only counterfactual ------------------------------------------------------------------------
def human_queue(items: list[dict], rate_per_h: float, start: datetime) -> list[dict]:
    """FIFO fluid queue: one request is finished every 1/rate hours of capacity. Items need `submitted` (datetime);
    returns them with `done` (datetime or None if not finished) - the caller decides the horizon."""
    free_at = start
    out = []
    for it in sorted(items, key=lambda x: x["submitted"]):
        begin = max(free_at, it["submitted"])
        done = begin + timedelta(hours=1 / rate_per_h) if rate_per_h > 0 else None
        free_at = done or free_at
        out.append({**it, "done": done})
    return out


def _breach_pct(items: list[dict], end: datetime) -> float:
    """Same definition as the KPI simulation: finished after target, or still open past target at the end."""
    breached = 0
    for it in items:
        finish = it["done"] if it["done"] and it["done"] <= end else None
        age = ((finish or end) - it["submitted"]).total_seconds() / 3600
        breached += age > it["target_h"]
    return _pct(breached, len(items))


def _sr_items(where: str = "") -> list[dict]:
    from app.data import db
    from app.runtime.clock import parse
    return [{"sr_id": r["sr_id"], "submitted": parse(r["submitted_ts"]), "target_h": r["sla_target_hrs"] or 24,
             "status": r["status"], "completed_ts": r["completed_ts"]}
            for r in db.query(f"SELECT * FROM service_requests WHERE origin!='seed' {where}")]


def _calibrate_diversion(items: list[dict], start: datetime, end: datetime, full_rate: float) -> tuple[float, float]:
    """Share of the SR team pulled onto the P1 that makes the AS-IS model reproduce the case's 84% breach."""
    lo, hi = 0.0, 0.99
    breach_at = lambda d: _breach_pct(human_queue(items, full_rate * (1 - d), start), end)  # noqa: E731
    if breach_at(lo) >= AS_IS["case_sr_breach_pct"]:
        return lo, breach_at(lo)
    for _ in range(30):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if breach_at(mid) < AS_IS["case_sr_breach_pct"] else (lo, mid)
    return round(hi, 3), breach_at(hi)


def v2_human_baseline() -> dict:
    from app.data import db, scenarios
    from app.kpi.simulate import _alert_metrics, _fresh, _triage_ms
    from app.runtime import player
    from app.runtime.clock import clock, parse
    A = AS_IS
    full_rate = A["sr_team_throughput_per_week"] / (7 * 24)          # SRs per hour, spread over the week
    handle_min = A["sr_team_size"] * A["sr_team_hours_per_week"] * 60 / A["sr_team_throughput_per_week"]

    # The P1 hour: people-minutes needed with and without the agents, same event stream.
    _fresh()
    rid = player.run_sync("licensecore_outage")
    m = _alert_metrics(rid)
    tri = _triage_ms()
    srs = _sr_items()
    agent_done = sum(1 for s in srs if s["status"] == "Completed")
    held = db.scalar("SELECT COUNT(*) FROM approvals WHERE status='pending'", default=0)
    as_is_min = {"alerts": m["raw_alerts"] * A["alert_review_min"], "triage": len(tri) * A["manual_triage_min"],
                 "srs": len(srs) * handle_min}
    to_be_min = {"alerts": m["surfaced"] * A["alert_review_min"], "triage": 0.0,
                 "srs": (len(srs) - agent_done) * handle_min, "approvals": held * A["approval_min"]}
    acks = m["acks_sim_s"]
    outage = {"raw_alerts": m["raw_alerts"], "surfaced_clusters": m["surfaced"], "incidents_triaged": len(tri),
              "srs": len(srs), "srs_agent_completed": agent_done, "held_for_approval": held,
              "people_min_as_is": {k: round(v) for k, v in as_is_min.items()},
              "people_min_to_be": {k: round(v) for k, v in to_be_min.items()},
              "people_min_freed": round(sum(as_is_min.values()) - sum(to_be_min.values())),
              "p1_ack_min_as_is": A["manual_ack_min"],
              "p1_ack_min_to_be": round(acks[0] / 60, 2) if acks else None}

    # The SR surge: 3,200 requests over 6 days while the team is on the P1.
    _fresh()
    sc = scenarios.build("sr_surge")
    start, end = sc.start, sc.start + timedelta(seconds=sc.duration_s)
    player.run_sync("sr_surge")
    clock.jump_to(end)
    items = _sr_items()
    diversion, as_is_breach = _calibrate_diversion(items, start, end, full_rate)
    rate = full_rate * (1 - diversion)
    as_is = human_queue(items, rate, start)
    left = [i for i in items if i["status"] != "Completed"]        # the agents' leftovers go to the same people
    to_be = human_queue(left, rate, start) + [{**i, "done": parse(i["completed_ts"])} for i in items
                                              if i["status"] == "Completed"]
    open_as_is = sum(1 for i in as_is if not i["done"] or i["done"] > end)
    open_to_be = sum(1 for i in to_be if not i["done"] or i["done"] > end)
    surge = {"srs": len(items), "calibrated_diversion_share": diversion, "human_rate_per_day": round(rate * 24, 1),
             "as_is": {"breach_pct": as_is_breach, "backlog_at_end": open_as_is, "human_handled": len(items)},
             "to_be": {"breach_pct": _breach_pct(to_be, end), "backlog_at_end": open_to_be, "human_handled": len(left),
                       "agent_completed": len(items) - len(left)},
             "case_breach_pct": A["case_sr_breach_pct"], "target_breach_pct": base.BY_KEY["sr_breach"]["target"],
             "people_hours_freed": round((len(items) - len(left)) * handle_min / 60, 1)}
    return {"assumptions": {**A, "derived_sr_handle_min": round(handle_min, 1)}, "p1_outage": outage, "sr_surge": surge}


# --- V3: sensitivity of the modelled KPIs -----------------------------------------------------------------
def _point(rows: list[dict], **kw) -> dict:
    from app.kpi.simulate import _case_scaled, _mttr_model
    success = kw.pop("runbook_success_pct", 100.0)
    mt = _mttr_model(kw, runbook_success_pct=success, rows=rows)
    s = _case_scaled(mt)
    out = {"mttr_h": round(s["mttr"], 1), "sla_compliance_pct": round(s["sla_compliance"], 1),
           "cost_m": round(s["cost"], 1)}
    out["targets_met"] = {"mttr": out["mttr_h"] < base.BY_KEY["mttr"]["target"],
                          "sla_compliance": out["sla_compliance_pct"] > base.BY_KEY["sla_compliance"]["target"],
                          "cost": out["cost_m"] < base.BY_KEY["cost"]["target"]}
    return out


def v3_sensitivity() -> dict:
    from app.kpi.simulate import _fresh, _mttr_rows
    _fresh()
    rows = _mttr_rows()
    default = {k: v[0] for k, v in SWEEP.items()}
    one_at_a_time = {k: [{"value": v, **_point(rows, **{**default, k: v})} for v in vals] for k, vals in SWEEP.items()}
    grid = [{"runbook_success_pct": s, "approval_latency_h": a,
             **_point(rows, **{**default, "runbook_success_pct": s, "approval_latency_h": a})}
            for s in SWEEP["runbook_success_pct"] for a in SWEEP["approval_latency_h"]]

    def break_even(kpi: str) -> float | None:
        """Lowest runbook success (1-pt steps, other assumptions at default) at which the target is met."""
        for s in range(0, 101):
            if _point(rows, **{**default, "runbook_success_pct": float(s)})["targets_met"][kpi]:
                return float(s)
        return None
    return {"incidents": len(rows), "runbook_matched": sum(r["runbook"] for r in rows), "default": default,
            "at_default": _point(rows, **default), "one_at_a_time": one_at_a_time, "grid": grid,
            "break_even_runbook_success_pct": {k: break_even(k) for k in ("mttr", "sla_compliance", "cost")}}


def run_validation() -> dict:
    from app.data import db
    from app.llm import client as llm
    db.configure()
    llm.set_client(None)
    t0 = time.time()
    res = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"), "llm_mode": "rules",
           "v1_excel_calibrated": v1_excel_calibrated(), "v2_human_baseline": v2_human_baseline(),
           "v3_sensitivity": v3_sensitivity()}
    res["duration_s"] = round(time.time() - t0, 1)
    return res


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the three validation scenarios (docs/validation/validation-scenarios.md)")
    ap.add_argument("--out", default=str(settings.DATA_DIR / "validation_results.json"))
    args = ap.parse_args()
    os.environ.setdefault("NEXA_DB_PATH", str(settings.DATA_DIR / "validation.db"))   # never the demo database
    os.environ.setdefault("NEXA_SIM_START", settings.CASE_START.isoformat())
    os.environ["LLM_MODE"] = "rules"
    res = run_validation()
    Path(args.out).write_text(json.dumps(res, indent=2, default=str))
    print(f"Validation finished in {res['duration_s']}s -> {args.out}")


if __name__ == "__main__":
    main()
