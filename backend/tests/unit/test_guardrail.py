"""Guardrail policy: every rule, pass and fail (docs/planning/implementation-plan.md §10)."""
import pytest

from app.agents import guardrail as gr
from app.graph.state import ActionProposal
from app.runtime.clock import parse


def resolve(**ctx) -> ActionProposal:
    base = {"priority": "P3", "app_tier": 3, "app_id": "NotificationService", "reversible": True, "kb_accuracy": 95.0,
            "blast_radius_users": 10}
    return ActionProposal(agent="incident_triage", action="resolve", entity_ref="INC1", summary="x",
                          confidence=0.92, context={**base, **ctx})


WEEKDAY = parse("2025-08-12T10:00:00")      # outside the freeze window


def test_all_rules_pass_approves(fresh):
    v = gr.evaluate(resolve(), WEEKDAY)
    assert v.verdict == "Approve" and not v.failed_rules
    assert {"min_confidence", "allowed_priorities", "max_app_tier_blocked", "require_reversible",
            "respect_freeze_windows", "min_kb_accuracy", "max_blast_radius_users"} <= set(v.passed_rules)


@pytest.mark.parametrize("ctx,rule", [
    ({"priority": "P1"}, "allowed_priorities"),
    ({"priority": "P2"}, "allowed_priorities"),
    ({"app_tier": 0}, "max_app_tier_blocked"),
    ({"app_tier": 1}, "max_app_tier_blocked"),
    ({"reversible": None}, "require_reversible"),
    ({"kb_accuracy": 89.9}, "min_kb_accuracy"),
    ({"kb_accuracy": None}, "min_kb_accuracy"),
    ({"blast_radius_users": 50}, "max_blast_radius_users"),
    ({"blast_radius_users": 1800}, "max_blast_radius_users"),
])
def test_each_rule_fails_to_require_approval(fresh, ctx, rule):
    v = gr.evaluate(resolve(**ctx), WEEKDAY)
    assert v.verdict == "RequireApproval"
    assert rule in v.failed_rules


def test_low_confidence_fails(fresh):
    p = resolve()
    p.confidence = 0.84
    v = gr.evaluate(p, WEEKDAY)
    assert v.verdict == "RequireApproval" and "min_confidence" in v.failed_rules


def test_boundary_confidence_passes(fresh):
    p = resolve()
    p.confidence = 0.85
    assert gr.evaluate(p, WEEKDAY).verdict == "Approve"


@pytest.mark.parametrize("day", [28, 31, 1, 4])
def test_freeze_window_days_fail(fresh, day):
    month = 7 if day >= 28 else 8
    v = gr.evaluate(resolve(), parse(f"2025-{month:02d}-{day:02d}T10:00:00"))
    assert "respect_freeze_windows" in v.failed_rules


def test_reopen_rate_rule(fresh):
    from app.tools import mock_servicenow as snow
    for i in range(10):
        snow.record_outcome("resolve", f"INC{i}", "reopened" if i < 2 else "ok")      # 20% > 5%
    v = gr.evaluate(resolve(), WEEKDAY)
    assert "max_reopen_rate_pct" in v.failed_rules


def test_reopen_rate_under_limit_passes(fresh):
    from app.tools import mock_servicenow as snow
    for i in range(40):
        snow.record_outcome("resolve", f"INC{i}", "reopened" if i == 0 else "ok")      # 2.5%
    assert gr.evaluate(resolve(), WEEKDAY).verdict == "Approve"


def test_irreversible_on_tier0_blocks(fresh):
    v = gr.evaluate(resolve(reversible=False, app_tier=0, app_id="LicenseCore"), WEEKDAY)
    assert v.verdict == "Block"


def test_irreversible_off_tier0_requires_approval_not_block(fresh):
    v = gr.evaluate(resolve(reversible=False, app_tier=2), WEEKDAY)
    assert v.verdict == "RequireApproval"


def test_privileged_access_never_autonomous(fresh):
    p = ActionProposal(agent="sr_fulfilment", action="grant_access", entity_ref="SR1", summary="x", confidence=0.98,
                       context={"privileged": True, "reversible": True, "blast_radius_users": 1})
    v = gr.evaluate(p, WEEKDAY)
    assert v.verdict == "RequireApproval" and "block_privileged_access" in v.failed_rules


def test_standard_access_and_reset_approved(fresh):
    for action in ("grant_access", "reset_password"):
        p = ActionProposal(agent="sr_fulfilment", action=action, entity_ref="SR1", summary="x", confidence=0.98,
                           context={"privileged": False, "reversible": True, "blast_radius_users": 1})
        assert gr.evaluate(p, WEEKDAY).verdict == "Approve"


def test_critical_alerts_never_suppressed(fresh):
    p = ActionProposal(agent="alert_correlation", action="suppress", entity_ref="CL-1", summary="x", confidence=0.99,
                       context={"severity": "Critical", "app_tier": 0})
    v = gr.evaluate(p, WEEKDAY)
    assert v.verdict == "RequireApproval" and "never_suppress_severities" in v.failed_rules


def test_noise_suppression_approved_even_on_tier0_context(fresh):
    p = ActionProposal(agent="alert_correlation", action="suppress", entity_ref="CL-1", summary="x", confidence=0.9,
                       context={"severity": "Low", "app_tier": 1, "blast_radius_users": 2100})
    assert gr.evaluate(p, WEEKDAY).verdict == "Approve"


def test_pass_through_actions_approved(fresh):
    for action in ("acknowledge", "open_incident", "classify", "route", "attach_kb", "escalate"):
        p = ActionProposal(agent="x", action=action, entity_ref="E", summary="x")
        assert gr.evaluate(p, WEEKDAY).verdict == "Approve"


def test_kb_updates_and_manual_review_always_human(fresh):
    for action in ("update_kb_article", "manual_review"):
        p = ActionProposal(agent="x", action=action, entity_ref="E", summary="x")
        v = gr.evaluate(p, WEEKDAY)
        assert v.verdict == "RequireApproval" and v.failed_rules == ["always_human_action"]


def test_unknown_action_fails_safe(fresh):
    v = gr.evaluate(ActionProposal(agent="x", action="launch_missiles", entity_ref="E", summary="x"), WEEKDAY)
    assert v.verdict == "RequireApproval" and v.failed_rules == ["unknown_action"]


def test_kill_switch_global_overrides_everything(fresh):
    from app.data import db
    db.set_setting("kill_switch", {"global": True, "agents": {}, "apps": {}})
    for action in ("acknowledge", "resolve", "reset_password"):
        p = ActionProposal(agent="x", action=action, entity_ref="E", summary="x", confidence=1.0,
                           context={"priority": "P4", "app_tier": 3, "reversible": True, "kb_accuracy": 99, "privileged": False,
                                    "blast_radius_users": 1})
        v = gr.evaluate(p, WEEKDAY)
        assert v.verdict == "RequireApproval" and v.failed_rules == ["kill_switch"]


def test_kill_switch_per_agent_and_per_app(fresh):
    from app.data import db
    db.set_setting("kill_switch", {"global": False, "agents": {"sr_fulfilment": True}, "apps": {"LicenseCore": True}})
    sr = ActionProposal(agent="sr_fulfilment", action="reset_password", entity_ref="SR1", summary="x",
                        context={"reversible": True, "blast_radius_users": 1})
    other = ActionProposal(agent="alert_correlation", action="acknowledge", entity_ref="CL", summary="x")
    app = ActionProposal(agent="incident_triage", action="classify", entity_ref="I", summary="x",
                         context={"app_id": "LicenseCore"})
    assert gr.evaluate(sr, WEEKDAY).failed_rules == ["kill_switch"]
    assert gr.evaluate(app, WEEKDAY).failed_rules == ["kill_switch"]
    assert gr.evaluate(other, WEEKDAY).verdict == "Approve"


def test_policy_is_configuration_not_code(fresh, tmp_path, monkeypatch):
    """GR-3: tightening the YAML changes verdicts with no code change."""
    from app import settings
    pol = settings.load_yaml("guardrail_policy")
    assert pol["version"] == 1 and pol["approve_only_if"]["min_confidence"] == 0.85
    p = resolve()
    p.confidence = 0.9
    assert gr.evaluate(p, WEEKDAY).verdict == "Approve"
    monkeypatch.setitem(pol["approve_only_if"], "min_confidence", 0.95)
    assert gr.evaluate(p, WEEKDAY).verdict == "RequireApproval"
