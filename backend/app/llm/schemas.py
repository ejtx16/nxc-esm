"""Structured-output schemas (docs/planning/implementation-plan.md §6). Constraints are enforced in code, not in the schema."""
from typing import Literal

from pydantic import BaseModel, Field

Category = Literal["Application Error", "Data Discrepancy", "Access Issue", "Performance Degradation",
                   "Integration Failure"]
Priority = Literal["P1", "P2", "P3", "P4"]


class AlertAssessment(BaseModel):
    cluster_id: str
    classification: Literal["true_positive", "false_positive", "redundant"]
    confidence: float = Field(description="0..1 probability the classification is correct")
    rationale: str


class TriageDecision(BaseModel):
    category: Category
    priority: Priority
    team: str
    confidence: float = Field(description="0..1")
    rationale: str
    similar_incident_ids: list[str]
    kb_article_ids: list[str]
    auto_resolvable: bool


class SRIntent(BaseModel):
    request_type: str
    target_system: str | None
    access_level: str | None
    eligible_for_self_service: bool
    rationale: str


class KBAssessment(BaseModel):
    accuracy_score: float = Field(description="0..100")
    recommended_action: Literal["keep", "update", "archive"]
    issues: list[str]
    rationale: str


class KBDraft(BaseModel):
    article_id: str
    updated_steps: list[str]
    change_summary: str
