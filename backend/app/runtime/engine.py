"""Runs the LangGraph orchestrator over queued events (single worker, in-process)."""
from __future__ import annotations

import threading
import time
import traceback

from app.data import audit, audit_ctx, db
from app.graph.build import build_graph
from app.runtime import bus
from app.runtime.clock import clock, parse
from app.tools import mock_servicenow as snow

_graph = None
_graph_lock = threading.Lock()
_sla_emitted: set[str] = set()
_proc_lock = threading.RLock()      # one processor at a time (worker thread, seed, tests)


def graph():
    global _graph
    with _graph_lock:
        if _graph is None:
            _graph = build_graph()
        return _graph


def _event_state(row: dict) -> dict:
    return {"event": {"event_id": row["event_id"], "type": row["type"], "entity_ref": row["entity_ref"],
                      "payload": db.loads(row["payload"], {}), "ts_sim": row["ts_sim"], "ts_real": row["ts_real"],
                      "run_id": row["run_id"]},
            "entity_ref": row["entity_ref"], "proposals": [], "verdicts": [], "signals": [], "trace": [],
            "results": [], "queued": []}


def process_event(row: dict) -> None:
    clock.jump_to(parse(row["ts_sim"]))
    with audit_ctx.start_trace(event_id=row["event_id"], run_id=row["run_id"], entity_ref=row["entity_ref"]):
        try:
            out = graph().invoke(_event_state(row))
            db.update("events", "event_id", row["event_id"], {"status": "done", "trace": db.dumps(out.get("trace", []))})
        except Exception as exc:  # noqa: BLE001 - OR-4: never block the flow; queue a manual item
            err = f"{type(exc).__name__}: {exc}"
            db.update("events", "event_id", row["event_id"], {"status": "failed", "error": err + "\n" + traceback.format_exc()[-800:]})
            _fail_to_human(row, err)


def _fail_to_human(row: dict, err: str) -> None:
    with audit_ctx.span("orchestrator", "fail_to_human", "orchestrator", entity_ref=row["entity_ref"],
                        event_type=row["type"]) as sp:
        sp.set_status("error", err)
        audit.log("orchestrator_failure", "orchestrator", row["entity_ref"], input_summary=row["type"],
                  reasoning=f"orchestrator error -> manual review: {err}", confidence=0.0, mode="error",
                  proposed_action={"action": "manual_review"}, verdict="RequireApproval",
                  run_id=row["run_id"], event_id=row["event_id"])
    db.execute(
        """INSERT INTO approvals (action, agent, entity_ref, summary, proposal, verdict, failed_rules, reasons, context,
           status, reason_code, created_ts, run_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("manual_review", "orchestrator", row["entity_ref"], f"Handle {row['entity_ref']} manually - orchestrator error",
         db.dumps({"action": "manual_review", "agent": "orchestrator", "entity_ref": row["entity_ref"], "params": {"error": err}}),
         "RequireApproval", db.dumps(["agent_failure"]), db.dumps([err]), "{}", "pending", "agent_failure",
         clock.now_iso(), row["run_id"]))


def process_pending(limit: int | None = None) -> int:
    n = 0
    with _proc_lock:
        while limit is None or n < limit:
            row = db.one("SELECT * FROM events WHERE status='new' ORDER BY event_id LIMIT 1")
            if row is None:
                break
            db.update("events", "event_id", row["event_id"], {"status": "processing"})
            process_event(row)
            n += 1
    return n


def pending_count() -> int:
    return db.scalar("SELECT COUNT(*) FROM events WHERE status IN ('new','processing')", default=0)


def sla_scan() -> int:
    """IT-5: refresh breach risk and queue an SLA check once an open incident passes the escalation fraction."""
    from app import settings
    frac_limit = settings.cfg()["escalate_at_fraction"]
    emitted = 0
    now = clock.now()
    for inc in db.query("SELECT * FROM incidents WHERE origin!='seed' AND status NOT IN ('Resolved','Closed')"):
        window = inc["sla_hours"] or snow.sla_hours(inc["priority"])
        elapsed = (now - parse(inc["opened_ts"] or inc["reported_date"])).total_seconds() / 3600
        frac = elapsed / window if window else 0
        if abs((inc["breach_risk"] or 0) - frac) > 0.01:
            db.update("incidents", "incident_id", inc["incident_id"], {"breach_risk": round(frac, 3)})
        if frac >= frac_limit and not inc["escalated"] and inc["incident_id"] not in _sla_emitted:
            _sla_emitted.add(inc["incident_id"])
            bus.publish(bus.INCIDENT_SLA_CHECK, inc["incident_id"], {"fraction": round(frac, 3)}, run_id=inc["run_id"])
            emitted += 1
    return emitted


def reset_state() -> None:
    _sla_emitted.clear()


class Worker(threading.Thread):
    def __init__(self) -> None:
        super().__init__(daemon=True, name="nexa-engine")
        self._halt = threading.Event()

    def run(self) -> None:
        last_scan = 0.0
        while not self._halt.is_set():
            try:
                if process_pending(50) == 0:
                    bus.wait(0.25)
                if time.monotonic() - last_scan > 1.0:
                    last_scan = time.monotonic()
                    sla_scan()
            except Exception:  # noqa: BLE001 - keep the worker alive
                traceback.print_exc()
                time.sleep(0.5)

    def stop(self) -> None:
        self._halt.set()
        bus.wake()
