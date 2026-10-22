"""In-process event bus (DL-2). Events are persisted in SQLite; a Condition wakes the worker."""
from __future__ import annotations

import threading
import time
from typing import Any

from app.data import db
from app.runtime.clock import clock, iso, parse

_cond = threading.Condition()
cancel = threading.Event()      # set during a data reset: long agent loops stop at the next item

# Event vocabulary (PRD DL-2 plus the cross-domain signals from OR-2).
ALERT_RECEIVED = "alert.received"
INCIDENT_CREATED = "incident.created"
INCIDENT_PRIORITY_CHANGED = "incident.priority_changed"
INCIDENT_SLA_CHECK = "incident.sla_check"
SR_SUBMITTED = "sr.submitted"
SR_SURGE_ON = "sr.surge_mode_on"
SR_SURGE_OFF = "sr.surge_mode_off"
KB_REVIEW = "kb.review"
KB_VALIDATE_RELATED = "kb.validate_related"
TRIAGE_HOLD = "triage.hold_reassignments"
TRIAGE_RELEASE = "triage.release_reassignments"


def publish(type: str, entity_ref: str, payload: dict[str, Any] | None = None, ts: Any = None,
            run_id: str | None = None) -> int:
    ts_sim = iso(parse(ts)) if ts is not None else clock.now_iso()
    eid = db.execute(
        "INSERT INTO events (type, entity_ref, payload, ts_sim, ts_real, status, run_id) VALUES (?,?,?,?,?,?,?)",
        (type, entity_ref, db.dumps(payload or {}), ts_sim, time.perf_counter(), "new", run_id),
    )
    with _cond:
        _cond.notify_all()
    return eid


def wait(timeout: float) -> None:
    with _cond:
        _cond.wait(timeout)


def wake() -> None:
    with _cond:
        _cond.notify_all()
