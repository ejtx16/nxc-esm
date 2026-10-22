"""Append-only audit trail helpers (DL-3)."""
from __future__ import annotations

from typing import Any

import time

from app.data import audit_ctx, db
from app.runtime.clock import clock


def log(kind: str, agent: str, entity_ref: str, *, reasoning: str = "", input_summary: str = "",
        confidence: float | None = None, proposed_action: Any = None, verdict: str | None = None,
        verdict_reason: str | None = None, human_decision: str | None = None, mode: str | None = None,
        latency_ms: float | None = None, parent_id: int | None = None, run_id: str | None = None,
        event_id: int | None = None) -> int:
    link = audit_ctx.ids()                         # trace/span/iteration of the active span (ADR-0004)
    did = db.execute(
        """INSERT INTO agent_decisions (parent_id, kind, run_id, event_id, agent, entity_ref, input_summary,
           reasoning, confidence, proposed_action, guardrail_verdict, guardrail_reason, human_decision, mode,
           latency_ms, timestamp, ts_real, trace_id, span_id, iteration)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (parent_id, kind, run_id or link.get("run_id"), event_id if event_id is not None else link.get("event_id"),
         agent, entity_ref, input_summary, reasoning, confidence,
         db.dumps(proposed_action) if proposed_action is not None else None, verdict, verdict_reason,
         human_decision, mode, latency_ms, clock.now_iso(), time.perf_counter(),
         link.get("trace_id"), link.get("span_id"), link.get("iteration")))
    sp = audit_ctx.current_span()
    if sp is not None:
        sp.link_decision(did)
    return did
