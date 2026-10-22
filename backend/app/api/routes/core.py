"""Health, overview (DB-1), activity feed (DB-2), operator controls (GR-5) and LLM status."""
from __future__ import annotations

from fastapi import APIRouter, Body

from app import settings
from app.agents import guardrail
from app.api.util import current_run, page, shape_all
from app.data import db, seed
from app.llm import client as llm
from app.runtime import bus, engine
from app.runtime.clock import clock

router = APIRouter()


@router.get("/health")
def health() -> dict:
    return {"ok": True, "db": str(db.db_file().name), "model": settings.MODEL, "llm_mode": llm.mode(),
            "pending_events": engine.pending_count(), "sim_now": clock.now_iso()}


@router.get("/overview")
def overview() -> dict:
    run = current_run()
    rid = run["run_id"] if run else None
    run_sql, run_args = ("AND run_id=?", (rid,)) if rid else ("AND 1=0", ())
    inc_open = db.query("SELECT priority, COUNT(*) n FROM incidents WHERE origin!='seed' AND status NOT IN "
                        "('Resolved','Closed') GROUP BY priority")
    raw = db.scalar(f"SELECT COUNT(*) FROM alerts WHERE origin='scenario' {run_sql}", run_args, default=0)
    clusters = db.scalar(f"SELECT COUNT(*) FROM clusters WHERE 1=1 {run_sql}", run_args, default=0)
    suppressed = db.scalar(f"SELECT COUNT(*) FROM clusters WHERE status='suppressed' {run_sql}", run_args, default=0)
    surfaced = db.query(f"SELECT truth_tp FROM clusters WHERE status IN ('surfaced','open','acknowledged','resolved') {run_sql}", run_args)
    noise_after = (100 * sum(1 for c in surfaced if not c["truth_tp"]) / len(surfaced)) if surfaced else None
    fp_raw = db.scalar(f"SELECT SUM(truth_fp) FROM alerts WHERE origin='scenario' {run_sql}", run_args, default=0)
    # Per-alert verdict vs the scenario's ground truth, so the noise KPI for this run can be checked.
    cls = db.one(f"SELECT COALESCE(SUM(classification='false_positive'), 0) noise, "
                 f"COALESCE(SUM((classification='false_positive') = (truth_fp=1)), 0) ok FROM alerts "
                 f"WHERE origin='scenario' AND classification IS NOT NULL {run_sql}", run_args)
    tp_hidden = db.scalar(f"SELECT COUNT(*) FROM clusters WHERE status='suppressed' AND truth_tp=1 {run_sql}",
                          run_args, default=0)
    ack = db.query(f"SELECT ack_latency_s, ack_real_ms FROM clusters WHERE ack_ts IS NOT NULL {run_sql}", run_args)
    sr = {r["status"]: r["n"] for r in db.query(f"SELECT status, COUNT(*) n FROM service_requests WHERE origin!='seed' GROUP BY status")}
    done = db.query("SELECT fulfilled_by FROM service_requests WHERE origin!='seed' AND status='Completed'")
    auto = sum(1 for r in done if (r["fulfilled_by"] or "").startswith("agent:"))
    kb = db.query("SELECT score, recommendation FROM kb_articles WHERE score IS NOT NULL")
    th = settings.cfg()["kb"]["accuracy_threshold"]
    appr = {r["status"]: r["n"] for r in db.query("SELECT status, COUNT(*) n FROM approvals GROUP BY status")}
    llm_stats = db.one("SELECT COUNT(*) calls, COALESCE(SUM(input_tokens),0) i, COALESCE(SUM(output_tokens),0) o, "
                       "COALESCE(SUM(cache_read),0) cr FROM llm_calls")
    sla = db.one("SELECT SUM(sla_met) m, SUM(sla_met)+SUM(sla_breached) t FROM sla_summary")
    at_risk = db.scalar("SELECT COUNT(*) FROM incidents WHERE origin!='seed' AND status NOT IN ('Resolved','Closed') "
                        "AND breach_risk>=0.5", default=0)
    resolved = db.scalar("SELECT COUNT(*) FROM incidents WHERE origin!='seed' AND status IN ('Resolved','Closed')", default=0)
    # A resolve that went through an approved Guardrail hold had a person in the loop; the rest ran unattended.
    auto_resolved = db.scalar("SELECT COUNT(*) FROM incidents WHERE origin!='seed' AND status IN ('Resolved','Closed') "
                              "AND incident_id NOT IN (SELECT entity_ref FROM approvals WHERE action='resolve' "
                              "AND status='executed')", default=0)
    with_person = db.scalar("SELECT COUNT(*) FROM incidents WHERE origin!='seed' AND status NOT IN ('Resolved','Closed') "
                            "AND assigned_agent LIKE 'On-call%'", default=0)
    # SLA for the current run only: closed tickets inside / outside their window, and open incidents already past it.
    inc_sla = db.one(f"SELECT COUNT(*) n, COALESCE(SUM(sla_breach='No'), 0) met FROM incidents WHERE origin!='seed' "
                     f"AND status IN ('Resolved','Closed') {run_sql}", run_args)
    sr_sla = db.one(f"SELECT COUNT(*) n, COALESCE(SUM(sla_met='Yes'), 0) met FROM service_requests WHERE origin!='seed' "
                    f"AND status='Completed' AND sla_met IS NOT NULL {run_sql}", run_args)
    overdue = db.scalar(f"SELECT COUNT(*) FROM incidents WHERE origin!='seed' AND status NOT IN ('Resolved','Closed') "
                        f"AND breach_risk>=1 {run_sql}", run_args, default=0)
    sla_n = inc_sla["n"] + sr_sla["n"]
    return {
        "clock": {"sim_now": clock.now_iso(), "speed": clock.speed, "paused": clock.paused},
        "sla": {"met_pct": round(100 * (inc_sla["met"] + sr_sla["met"]) / sla_n, 1) if sla_n else None,
                "incidents_met": inc_sla["met"], "incidents_closed": inc_sla["n"],
                "requests_met": sr_sla["met"], "requests_closed": sr_sla["n"], "open_overdue": overdue},
        "run": run,
        "surge_mode": bool(db.get_setting("surge_mode")), "triage_hold": bool(db.get_setting("triage_hold")),
        "kill_switch": guardrail.kill_switch_state(), "llm_mode": llm.mode(),
        "incidents": {"open_by_priority": {r["priority"]: r["n"] for r in inc_open},
                      "open_total": sum(r["n"] for r in inc_open), "at_risk": at_risk,
                      "resolved": resolved, "auto_resolved": auto_resolved, "with_person": with_person,
                      "historical_sla_pct": round(100 * sla["m"] / sla["t"], 1) if sla and sla["t"] else None},
        "alerts": {"raw": raw, "clusters": clusters, "suppressed_clusters": suppressed, "noise_alerts": cls["noise"],
                   "classified_correctly": cls["ok"], "real_suppressed": tp_hidden,
                   "raw_noise_pct": round(100 * fp_raw / raw, 1) if raw else None,
                   "surfaced_noise_pct": round(noise_after, 1) if noise_after is not None else None,
                   "ack_avg_s": round(sum(a["ack_latency_s"] for a in ack) / len(ack), 1) if ack else None,
                   "ack_avg_real_ms": round(sum(a["ack_real_ms"] for a in ack) / len(ack), 1) if ack else None},
        "sr": {"by_status": sr, "completed": len(done), "auto_fulfilled": auto,
               "pending_human": sr.get("Pending Human", 0), "pending_approval": sr.get("Pending Approval", 0)},
        "kb": {"articles": len(kb), "mean_score": round(sum(k["score"] for k in kb) / len(kb), 1) if kb else None,
               "mean_completeness": db.scalar("SELECT ROUND(AVG(completeness_score_pct), 1) FROM kb_articles "
                                              "WHERE completeness_score_pct IS NOT NULL"),
               "below_threshold": sum(1 for k in kb if k["score"] < th), "threshold": th,
               "pending_drafts": db.scalar("SELECT COUNT(*) FROM kb_drafts WHERE status='pending'", default=0)},
        "approvals": {"pending": appr.get("pending", 0), "blocked": appr.get("blocked", 0),
                      "executed": appr.get("executed", 0), "rejected": appr.get("rejected", 0)},
        "llm": {"calls": llm_stats["calls"], "input_tokens": llm_stats["i"], "output_tokens": llm_stats["o"],
                "cache_read_tokens": llm_stats["cr"]},
        "pending_events": engine.pending_count(),
    }


@router.get("/notifications")
def notifications(limit: int = 20) -> list[dict]:
    """The dummy notices (resolved / assigned) the console shows as pop-ups, newest first."""
    rows = db.query("SELECT span_id, entity_ref, output_json FROM audit_spans WHERE name LIKE 'mock_servicenow.notify_%' "
                    "ORDER BY started_real DESC LIMIT ?", (limit,))
    return [{"id": r["span_id"], "incident_id": r["entity_ref"], **db.loads(r["output_json"], {})} for r in rows]


@router.get("/feed")
def feed(since_id: int = 0, limit: int | None = None, kind: str | None = None, agent: str | None = None,
         entity: str | None = None) -> list[dict]:
    """Agent activity feed (DB-2), newest first. Use since_id for incremental polling."""
    where, args = ["decision_id>?"], [since_id]
    if kind:
        where.append("kind=?"); args.append(kind)
    if agent:
        where.append("agent=?"); args.append(agent)
    if entity:
        where.append("entity_ref=?"); args.append(entity)
    rows = db.query(f"SELECT * FROM agent_decisions WHERE {' AND '.join(where)} ORDER BY decision_id DESC LIMIT ?",
                    [*args, page(limit, 80, 500)])
    return shape_all(rows, ("proposed_action",))


@router.get("/decisions/{entity_ref}")
def decisions(entity_ref: str) -> list[dict]:
    """Full audit chain for one entity (agent -> guardrail -> execution -> human)."""
    return shape_all(db.query("SELECT * FROM agent_decisions WHERE entity_ref=? ORDER BY decision_id", (entity_ref,)),
                     ("proposed_action",))


@router.get("/settings")
def get_settings() -> dict:
    return {"kill_switch": guardrail.kill_switch_state(), "llm_mode": llm.mode(),
            "llm_mode_env": settings.env_llm_mode(), "surge_mode": bool(db.get_setting("surge_mode")),
            "triage_hold": bool(db.get_setting("triage_hold")), "model": settings.MODEL,
            "policy": guardrail.policy(), "apps": db.query("SELECT app_id, tier FROM apps ORDER BY tier, app_id")}


@router.post("/settings/kill-switch")
def kill_switch(body: dict = Body(...)) -> dict:
    """GR-5: disable autonomous actions globally, per agent or per application."""
    ks = guardrail.kill_switch_state()
    if "global" in body:
        ks["global"] = bool(body["global"])
    if "agent" in body:
        ks["agents"][body["agent"]] = bool(body.get("on", True))
    if "app" in body:
        ks["apps"][body["app"]] = bool(body.get("on", True))
    db.set_setting("kill_switch", ks)
    return ks


@router.post("/settings/surge")
def surge(body: dict = Body(...)) -> dict:
    from app.runtime import bus
    on = bool(body.get("on"))
    if on:
        db.set_setting("surge_mode", True)
        bus.publish(bus.SR_SURGE_ON, "MANUAL", {"reason": "operator"})
        bus.publish(bus.TRIAGE_HOLD, "MANUAL", {})
    else:
        bus.publish(bus.SR_SURGE_OFF, "MANUAL", {"reason": "operator"})
    return {"requested": "on" if on else "off"}


@router.get("/llm/usage")
def llm_usage(run_id: str | None = None) -> dict:
    where, args = ("WHERE run_id=?", (run_id,)) if run_id else ("", ())
    return {"mode": llm.mode(), "model": settings.MODEL,
            "by_agent": db.query("SELECT agent, COUNT(*) calls, SUM(input_tokens) input_tokens, SUM(output_tokens) "
                                 "output_tokens, SUM(cache_read) cache_read, ROUND(AVG(latency_ms)) avg_ms, "
                                 "ROUND(SUM(cost_usd), 6) cost_usd, SUM(CASE WHEN attempt > 1 THEN 1 ELSE 0 END) retries, "
                                 f"SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) errors FROM llm_calls {where} "
                                 "GROUP BY agent", args),
            "recent": db.query(f"SELECT * FROM llm_calls {where} ORDER BY id DESC LIMIT 20", args)}


@router.post("/llm/health-check")
def llm_health() -> dict:
    return llm.health_check()


@router.get("/graph")
def graph_diagram() -> dict:
    return {"mermaid": engine.graph().get_graph().draw_mermaid()}


@router.post("/admin/reset")
def reset(body: dict = Body(default={})) -> dict:
    from app.runtime import player
    if player.Player.current is not None and player.Player.current.is_alive():
        player.Player.current.stop()
    # Stop any agent work still in flight and wait for it, so nothing from before the reset lands in the new data.
    bus.cancel.set()
    with engine._proc_lock:
        bus.cancel.clear()
        clock.reset()
        engine.reset_state()
        counts = seed.run(reset=True)
    return {"ok": True, "seeded": counts}
