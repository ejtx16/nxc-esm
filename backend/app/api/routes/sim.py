"""Scenario controls and KPI simulation (D1, D6)."""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException

from app.api.util import current_run
from app.data import db, scenarios
from app.runtime import player
from app.runtime.clock import clock

router = APIRouter()


def _status() -> dict:
    p = player.Player.current
    run = current_run()
    alive = bool(p and p.is_alive())
    return {"active": alive, "paused": bool(alive and p.paused), "run": run, "sim_now": clock.now_iso(),
            "speed": clock.speed, "emitted": p.emitted if p else 0,
            "total": p.sc.total if p else (run or {}).get("total_events"),
            "scenario": p.sc.name if p else (run or {}).get("scenario")}


@router.get("/scenarios")
def list_scenarios() -> dict:
    return {"scenarios": scenarios.catalogue(), "status": _status()}


@router.get("/scenarios/status")
def status() -> dict:
    return _status()


@router.post("/scenarios/{name}/start")
def start(name: str, body: dict = Body(default={})) -> dict:
    try:
        player.start_live(name, body.get("speed"))
    except KeyError:
        raise HTTPException(404, f"unknown scenario '{name}'")
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    return _status()


@router.post("/scenarios/pause")
def pause() -> dict:
    if player.Player.current and player.Player.current.is_alive():
        player.Player.current.pause()
    return _status()


@router.post("/scenarios/resume")
def resume() -> dict:
    if player.Player.current and player.Player.current.is_alive():
        player.Player.current.resume()
    return _status()


@router.post("/scenarios/stop")
def stop() -> dict:
    if player.Player.current and player.Player.current.is_alive():
        player.Player.current.stop()
    return _status()


@router.post("/scenarios/speed")
def speed(body: dict = Body(...)) -> dict:
    clock.set_speed(float(body["speed"]))
    return _status()


@router.get("/kpis")
def kpis() -> dict:
    from app.kpi import simulate
    return simulate.latest()


@router.post("/kpis/simulate")
def kpis_simulate() -> dict:
    from app.kpi import simulate
    return simulate.start_background()


@router.get("/kpis/excel")
def kpis_excel() -> dict:
    from app.kpi import excel_check
    return excel_check.latest()


@router.post("/kpis/excel/run")
def kpis_excel_run() -> dict:
    from app.kpi import excel_check
    return excel_check.start_background()


@router.get("/kpis/run")
def kpis_run() -> dict:
    """KPIs measured on the latest scenario run, against the case baselines. Only KPIs the run produced data for."""
    from app.api.routes import core
    from app.kpi import baseline as base
    run = current_run()
    if not run:
        return {"run": None, "kpis": []}
    rid = (run["run_id"],)
    a = core.overview()["alerts"]

    def entry(key: str, after: float, method: str, detail: dict, display: str | None = None) -> dict:
        b = base.BY_KEY[key]
        met = after <= b["target"] if b["better"] == "lower" else after >= b["target"]
        return {**b, "after": round(after, 3), "after_display": display, "target_met": met, "kind": "measured",
                "method": method, "detail": detail}

    out = []
    if a["surfaced_noise_pct"] is not None:
        out.append(entry("alert_noise", a["surfaced_noise_pct"],
                         "Noise alert groups still shown to people / all groups shown, checked against the alert log's labels",
                         {"alerts_received": a["raw"], "noise_before_pct": a["raw_noise_pct"],
                          "classified_as_noise": a["noise_alerts"], "classified_correctly": a["classified_correctly"],
                          "groups_after_dedupe": a["clusters"], "groups_suppressed": a["suppressed_clusters"],
                          "real_alerts_wrongly_suppressed": a["real_suppressed"]}))
    if a["ack_avg_real_ms"] is not None:
        ms = a["ack_avg_real_ms"]
        out.append(entry("ack_time", ms / 60000, "Processing time from an alert arriving to the agent acknowledging it",
                         {"mean_ms": ms}, f"{ms / 1000:.2f} s"))
    tri = [r["triage_ms"] for r in db.query("SELECT triage_ms FROM incidents WHERE run_id=? AND triage_ms IS NOT NULL", rid)]
    if tri:
        ms = sum(tri) / len(tri)
        out.append(entry("triage_time", ms / 60000, "Time from an incident being opened to the triage decision",
                         {"incidents_triaged": len(tri), "mean_ms": round(ms, 1)}, f"{ms / 1000:.2f} s"))
    srs = db.query("SELECT status, sla_met, fulfilled_by FROM service_requests WHERE run_id=?", rid)
    if srs:
        auto = sum(1 for r in srs if (r["fulfilled_by"] or "").startswith("agent:"))
        out.append(entry("self_service", 100 * auto / len(srs), "Requests fulfilled by the agent / all requests in the run",
                         {"requests": len(srs), "fulfilled_by_agent": auto}))
        done = [r for r in srs if r["status"] == "Completed" and r["sla_met"] is not None]
        if done:
            late = sum(1 for r in done if r["sla_met"] != "Yes")
            out.append(entry("sr_breach", 100 * late / len(done), "Completed requests past their SLA / completed requests",
                             {"completed": len(done), "breached": late}))
    return {"run": run, "kpis": out}
