"""Case-study baselines and targets (docs/product/business-problem.md §3). The dummy data does not reproduce these, so they are quoted."""

KPIS = [
    {"key": "alert_noise", "label": "Alert noise ratio", "unit": "%", "baseline": 53, "target": 10, "better": "lower",
     "target_text": "< 10%", "goal": "G2"},
    {"key": "ack_time", "label": "Critical alert ack time", "unit": "min", "baseline": 22, "target": 3, "better": "lower",
     "target_text": "< 3 min", "goal": "G3"},
    {"key": "triage_time", "label": "Avg triage time", "unit": "min", "baseline": 18, "target": 2, "better": "lower",
     "target_text": "< 2 min", "goal": "G9"},
    {"key": "self_service", "label": "Self-service rate", "unit": "%", "baseline": 4, "target": 40, "better": "higher",
     "target_text": "> 40%", "goal": "G6"},
    {"key": "sr_breach", "label": "SR SLA breach", "unit": "%", "baseline": 84, "target": 5, "better": "lower",
     "target_text": "< 5%", "goal": "G5"},
    {"key": "kb_accuracy", "label": "KB article accuracy", "unit": "%", "baseline": 62, "target": 90, "better": "higher",
     "target_text": "> 90%", "goal": "G7"},
    {"key": "kb_escalation", "label": "KB escalation rate", "unit": "%", "baseline": 64, "target": 15, "better": "lower",
     "target_text": "< 15%", "goal": "G8"},
    {"key": "mttr", "label": "P1/P2 MTTR", "unit": "hrs", "baseline": 48, "target": 4, "better": "lower",
     "target_text": "< 4 hrs", "goal": "G1"},
    {"key": "sla_compliance", "label": "P1/P2 SLA compliance", "unit": "%", "baseline": 33, "target": 95,
     "better": "higher", "target_text": "> 95%", "goal": "G4"},
    {"key": "cost", "label": "Operational cost impact", "unit": "$M", "baseline": 31, "target": 3, "better": "lower",
     "target_text": "< $3M", "goal": "G10"},
]

BY_KEY = {k["key"]: k for k in KPIS}

# Assumptions behind the MODELLED KPIs. Shown on the KPI page; edit here to run what-ifs.
ASSUMPTIONS = {
    "triage_delay_saved_h": 0.30,          # 18 min manual triage removed (case baseline)
    "ack_lag_saved_h": 0.37,               # 22 min alert acknowledgement lag removed for alert-originated P1s
    "escalation_lag_saved_h": 0.58,        # 35 min post-breach escalation lag removed (escalate at 50% of SLA window)
    "first_time_right_routing_pct": 25.0,  # skills-based routing removes this share of hand-off time (assumed)
    "approval_latency_h": 0.25,            # a human approves a prepared runbook fix in ~15 min (Guardrail keeps P1/P2 human)
    "runbook_fix_h": 0.1,                  # runbook execution time
    "kb_escalation_reduction_pct": 50.0,   # updated articles escalate half as often (config kb.post_update_escalation_factor)
    "kb_updates_approved": "all drafted updates are approved by a knowledge manager (modelled)",
}
