"""Helpers shared by agent nodes."""
from __future__ import annotations

import time
from typing import Any, Iterable

from app.data import audit, db
from app.graph.state import ActionProposal, OpsState


def prop(agent: str, action: str, entity_ref: str, summary: str, *, confidence: float = 1.0,
         params: dict | None = None, context: dict | None = None) -> ActionProposal:
    return ActionProposal(agent=agent, action=action, entity_ref=entity_ref, summary=summary,
                          confidence=round(float(confidence), 3), params=params or {}, context=context or {})


def log_decision(state: OpsState, agent: str, entity_ref: str, reasoning: str, *, input_summary: str = "",
                 confidence: float | None = None, proposals: Iterable[ActionProposal] = (), mode: str = "rules",
                 t0: float | None = None) -> int:
    """Write the agent's decision row and link its proposals to it (DL-3)."""
    props = list(proposals)
    ev = state.get("event", {})
    did = audit.log("agent", agent, entity_ref, reasoning=reasoning, input_summary=input_summary,
                    confidence=confidence, mode=mode, parent_id=None,
                    proposed_action=[{"action": p.action, "summary": p.summary, "confidence": p.confidence}
                                     for p in props] or None,
                    latency_ms=round((time.perf_counter() - t0) * 1000, 2) if t0 else None,
                    run_id=ev.get("run_id"), event_id=ev.get("event_id"))
    for p in props:
        p.parent_decision_id = did
    return did


def app_facts(app_id: str | None) -> dict[str, Any]:
    info = db.one("SELECT * FROM apps WHERE app_id=?", (app_id,)) if app_id else None
    if not info:
        return {"app_id": app_id}
    return {"app_id": app_id, "app_tier": info["tier"], "blast_radius_users": info["users_affected"]}
