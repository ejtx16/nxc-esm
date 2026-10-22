"""LangGraph state and the typed objects that flow between nodes."""
from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, TypedDict
from uuid import uuid4

from pydantic import BaseModel, Field

Verdict = Literal["Approve", "RequireApproval", "Block"]


class ActionProposal(BaseModel):
    """An action an agent wants to take. Every proposal passes the Guardrail (GR-1)."""
    proposal_id: str = Field(default_factory=lambda: uuid4().hex[:10])
    agent: str
    action: str
    entity_ref: str
    summary: str
    confidence: float = 1.0
    params: dict[str, Any] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)      # facts the policy evaluates
    parent_decision_id: int | None = None


class GuardrailVerdict(BaseModel):
    proposal_id: str
    verdict: Verdict
    failed_rules: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    passed_rules: list[str] = Field(default_factory=list)
    policy_version: int = 1
    decision_id: int | None = None      # audit row written for this verdict

    @property
    def risk(self) -> str:
        """Approve = low risk (runs unattended); anything held for a person is high risk."""
        return "low" if self.verdict == "Approve" else "high"


class Event(TypedDict, total=False):
    event_id: int
    type: str
    entity_ref: str
    payload: dict
    ts_sim: str
    ts_real: float
    run_id: str | None


class OpsState(TypedDict, total=False):
    event: Event
    entity_ref: str
    proposals: Annotated[list[ActionProposal], operator.add]
    verdicts: Annotated[list[GuardrailVerdict], operator.add]
    signals: Annotated[list[str], operator.add]              # e.g. "p1_surge"
    trace: Annotated[list[str], operator.add]                # human-readable steps for the UI
    results: Annotated[list[dict], operator.add]
    queued: Annotated[list[int], operator.add]               # approval ids created this pass
