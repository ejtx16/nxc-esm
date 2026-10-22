"""Scenario player: replays scenario events on the sim clock (live thread) or synchronously (tests / KPI simulation)."""
from __future__ import annotations

import threading
import time
from datetime import timedelta

from app.data import db, scenarios
from app.runtime import bus, engine
from app.runtime.clock import clock, iso
from app.tools import mock_servicenow as snow


def _emit(ev: scenarios.SEvent, run_id: str, start) -> None:
    ts = iso(start + timedelta(seconds=ev.t))
    d = ev.data
    if ev.kind == "alert":
        aid = snow.next_alert_id()
        db.insert("alerts", {"alert_id": aid, "alert_name": d["name"], "source_system": d["system"],
                             "severity": d["severity"], "alert_ts": ts, "acknowledged": "No",
                             "true_positive": "No" if d["fp"] else "Yes", "incident_created": "No", "auto_resolved": "No",
                             "origin": "scenario", "run_id": run_id, "status": "new", "truth_fp": 1 if d["fp"] else 0,
                             "self_recovered": 1 if d["recovered"] else 0})
        bus.publish(bus.ALERT_RECEIVED, aid, {}, ts=ts, run_id=run_id)
    elif ev.kind == "sr":
        emp = db.one("SELECT department FROM employees WHERE employee_id=?", (d["requestor_id"],)) or {}
        sid = snow.next_sr_id()
        db.insert("service_requests", {
            "sr_id": sid, "request_type": d["request_type"], "requestor_id": d["requestor_id"],
            "department": emp.get("department"), "submitted_ts": ts, "priority": "Medium", "status": "New",
            "origin": "scenario", "run_id": run_id, "channel": d["channel"], "target_system": d["target_system"],
            "access_level": d["access_level"], "text": d["text"],
            "sla_target_hrs": snow.sr_sla_target(d["request_type"]) if d["request_type"] else None})
        bus.publish(bus.SR_SUBMITTED, sid, {}, ts=ts, run_id=run_id)
    elif ev.kind == "incident":
        iid = snow.next_incident_id()
        db.insert("incidents", {"incident_id": iid, "priority": "P3", "provisional_priority": "P3",
                                "short_description": d["description"], "reported_by": d["reported_by"],
                                "reported_date": ts[:10], "status": "Open", "origin": "scenario", "run_id": run_id,
                                "sla_hours": snow.sla_hours("P3"), "opened_ts": ts, "escalated": 0, "held": 0,
                                "resolution_notes": "Awaiting triage"})
        bus.publish(bus.INCIDENT_CREATED, iid, {"source": "ticket"}, ts=ts, run_id=run_id)
    elif ev.kind == "signal":
        bus.publish(d["type"], d["entity"], d.get("payload"), ts=ts, run_id=run_id)


def _begin(sc: scenarios.Scenario, speed: float, keep_state: bool = False) -> str:
    n = db.scalar("SELECT COUNT(*) FROM runs", default=0) + 1
    run_id = f"RUN-{n:03d}"
    if not keep_state:
        db.set_setting("surge_mode", False)
        db.set_setting("triage_hold", False)
        db.set_setting("escalate_repeats", None)     # opt-in per scenario (json_alerts)
    engine.reset_state()
    clock.reset(sc.start, speed)
    db.insert("runs", {"run_id": run_id, "scenario": sc.name, "speed": speed, "status": "running",
                       "started_real": time.time(), "started_sim": iso(sc.start), "duration_sim_s": sc.duration_s,
                       "total_events": sc.total, "done_events": 0})
    if sc.setup:
        sc.setup(run_id)
    return run_id


def run_sync(name: str, seed: int | None = None) -> str:
    """Replay a whole scenario without sleeping, processing events as they are emitted."""
    sc = scenarios.build(name, seed)
    run_id = _begin(sc, 1.0)
    engine.process_pending()
    for i, ev in enumerate(sc.events, 1):
        clock.jump_to(sc.start + timedelta(seconds=ev.t))
        _emit(ev, run_id, sc.start)
        engine.process_pending()
        if i % 200 == 0:
            db.update("runs", "run_id", run_id, {"done_events": i})
    db.update("runs", "run_id", run_id, {"done_events": sc.total, "status": "finished"})
    return run_id


class Player(threading.Thread):
    """Live replay: emits events when the sim clock reaches their timestamp, so speed/pause apply instantly."""
    current: "Player | None" = None

    def __init__(self, name: str, speed: float | None = None, seed: int | None = None) -> None:
        super().__init__(daemon=True, name=f"player-{name}")
        self.sc = scenarios.build(name, seed)
        self.speed = speed or self.sc.default_speed
        self.run_id = _begin(self.sc, self.speed)
        self._halt = threading.Event()
        self.paused = False
        self.emitted = 0

    def run(self) -> None:
        try:
            for ev in self.sc.events:
                target = self.sc.start + timedelta(seconds=ev.t)
                while not self._halt.is_set() and (self.paused or clock.now() < target):
                    time.sleep(0.02)
                if self._halt.is_set():
                    break
                _emit(ev, self.run_id, self.sc.start)
                self.emitted += 1
                if self.emitted % 25 == 0:
                    db.update("runs", "run_id", self.run_id, {"done_events": self.emitted})
            else:
                db.update("runs", "run_id", self.run_id, {"done_events": self.emitted, "status": "finished"})
                return
            db.update("runs", "run_id", self.run_id, {"done_events": self.emitted, "status": "stopped"})
        except Exception as exc:  # noqa: BLE001
            db.update("runs", "run_id", self.run_id, {"status": f"error: {exc}"})

    def pause(self) -> None:
        self.paused = True
        clock.pause()
        db.update("runs", "run_id", self.run_id, {"status": "paused"})

    def resume(self) -> None:
        self.paused = False
        clock.resume()
        db.update("runs", "run_id", self.run_id, {"status": "running"})

    def stop(self) -> None:
        self._halt.set()


def start_live(name: str, speed: float | None = None, seed: int | None = None) -> Player:
    if Player.current is not None and Player.current.is_alive():
        raise RuntimeError("a scenario is already running - stop it first")
    db.clear_audit()  # each live simulation starts with an empty audit trail
    p = Player(name, speed, seed)
    Player.current = p
    p.start()
    return p
