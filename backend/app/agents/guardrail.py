"""Guardrail Agent (GR-1..GR-5): deterministic policy evaluation. No LLM is involved by design (ADR-0001)."""
from __future__ import annotations

from typing import Any

from app import settings
from app.data import db
from app.graph.state import ActionProposal, GuardrailVerdict
from app.runtime.clock import clock, parse
from app.tools import mock_servicenow as snow


def policy() -> dict:
    return settings.load_yaml("guardrail_policy")


def kill_switch_state() -> dict:
    ks = db.get_setting("kill_switch") or {}
    return {"global": bool(ks.get("global")), "agents": ks.get("agents", {}), "apps": ks.get("apps", {})}


def kill_switch_active(proposal: ActionProposal) -> str | None:
    ks = kill_switch_state()
    if ks["global"]:
        return "global kill switch is ON"
    if ks["agents"].get(proposal.agent):
        return f"kill switch ON for the {proposal.agent} agent"
    app = proposal.context.get("app_id")
    if app and ks["apps"].get(app):
        return f"kill switch ON for {app}"
    return None


def in_freeze_window(now) -> str | None:
    for w in policy().get("freeze_windows", []):
        if now.day in w.get("days_of_month", []):
            return w["name"]
    return None


def _scope_ok(rule: str, action: str, pol: dict) -> bool:
    scope = pol.get("rule_scope", {}).get(rule)
    return scope is None or action in scope


def evaluate(proposal: ActionProposal, now=None) -> GuardrailVerdict:
    pol = policy()
    now = now or clock.now()
    version = int(pol.get("version", 1))
    action, ctx = proposal.action, proposal.context
    failed: list[str] = []
    reasons: list[str] = []
    passed: list[str] = []

    def fail(rule: str, msg: str) -> None:
        failed.append(rule)
        reasons.append(msg)

    ks = kill_switch_active(proposal)
    if ks:                                                     # GR-5: overrides everything
        return GuardrailVerdict(proposal_id=proposal.proposal_id, verdict="RequireApproval",
                                failed_rules=["kill_switch"], reasons=[ks], policy_version=version)

    if action in pol.get("always_human_actions", []):
        return GuardrailVerdict(proposal_id=proposal.proposal_id, verdict="RequireApproval",
                                failed_rules=["always_human_action"],
                                reasons=[f"'{action}' is never autonomous"], policy_version=version)

    if action in pol.get("pass_through_actions", []):
        return GuardrailVerdict(proposal_id=proposal.proposal_id, verdict="Approve",
                                passed_rules=["pass_through_action"],
                                reasons=["non-mutating coordination action"], policy_version=version)

    rules: dict[str, Any] = pol["approve_only_if"]
    known_gated = {"resolve", "remediate", "suppress", "reset_password", "grant_access"}
    if action not in known_gated:
        return GuardrailVerdict(proposal_id=proposal.proposal_id, verdict="RequireApproval",
                                failed_rules=["unknown_action"],
                                reasons=[f"no policy defined for action '{action}' - failing safe"],
                                policy_version=version)

    def applies(rule: str) -> bool:
        return rule in rules and _scope_ok(rule, action, pol)

    # Confidence
    if applies("min_confidence"):
        if proposal.confidence < rules["min_confidence"]:
            fail("min_confidence", f"confidence {proposal.confidence:.2f} < {rules['min_confidence']}")
        else:
            passed.append("min_confidence")
    # Priority
    if applies("allowed_priorities") and ctx.get("priority"):
        if ctx["priority"] not in rules["allowed_priorities"]:
            fail("allowed_priorities", f"{ctx['priority']} incidents always need a human "
                                       f"(auto allowed only for {', '.join(rules['allowed_priorities'])})")
        else:
            passed.append("allowed_priorities")
    # Application tier
    tier = ctx.get("app_tier")
    if applies("max_app_tier_blocked") and tier is not None:
        if tier <= rules["max_app_tier_blocked"]:
            fail("max_app_tier_blocked", f"{ctx.get('app_id', 'application')} is Tier {tier} "
                                         f"(tiers 0-{rules['max_app_tier_blocked']} need a human)")
        else:
            passed.append("max_app_tier_blocked")
    # Reversibility
    if applies("require_reversible"):
        if ctx.get("reversible") is False:
            fail("require_reversible", "action is not reversible / has no rollback")
        elif ctx.get("reversible") is None and action in ("resolve", "remediate"):
            fail("require_reversible", "reversibility of the action is unknown")
        else:
            passed.append("require_reversible")
    # Freeze windows
    if applies("respect_freeze_windows") and rules["respect_freeze_windows"]:
        w = in_freeze_window(now)
        if w:
            fail("respect_freeze_windows", f"inside change freeze '{w}' ({now:%b %d})")
        else:
            passed.append("respect_freeze_windows")
    # Knowledge basis
    if applies("min_kb_accuracy"):
        acc = ctx.get("kb_accuracy")
        if acc is None:
            fail("min_kb_accuracy", "no runbook / KB article backs this action")
        elif acc < rules["min_kb_accuracy"]:
            fail("min_kb_accuracy", f"backing KB article {ctx.get('kb_article', '')} accuracy {acc:.0f}% "
                                    f"< {rules['min_kb_accuracy']}%")
        else:
            passed.append("min_kb_accuracy")
    # Blast radius
    if applies("max_blast_radius_users") and ctx.get("blast_radius_users") is not None:
        if ctx["blast_radius_users"] >= rules["max_blast_radius_users"]:
            fail("max_blast_radius_users", f"affects ~{ctx['blast_radius_users']} users "
                                           f"(limit < {rules['max_blast_radius_users']})")
        else:
            passed.append("max_blast_radius_users")
    # History (reopen / failure rate for this action type)
    if applies("max_reopen_rate_pct"):
        rate = snow.reopen_rate_pct(action, int(pol.get("history_window_days", 30)))
        if rate is not None and rate > rules["max_reopen_rate_pct"]:
            fail("max_reopen_rate_pct", f"'{action}' reopen rate {rate:.0f}% > {rules['max_reopen_rate_pct']}%")
        else:
            passed.append("max_reopen_rate_pct")
    # Privilege
    if applies("block_privileged_access") and rules["block_privileged_access"]:
        if ctx.get("privileged"):
            fail("block_privileged_access", "privileged/admin access is never granted autonomously")
        else:
            passed.append("block_privileged_access")
    # Never suppress certain severities
    if applies("never_suppress_severities"):
        if ctx.get("severity") in rules["never_suppress_severities"]:
            fail("never_suppress_severities", f"{ctx.get('severity')} alerts are never auto-suppressed")
        else:
            passed.append("never_suppress_severities")

    if not failed:
        return GuardrailVerdict(proposal_id=proposal.proposal_id, verdict="Approve", passed_rules=passed,
                                reasons=["all policy rules passed"], policy_version=version)

    verdict = "RequireApproval"
    if "irreversible_on_tier_0" in pol.get("block_if", []) and ctx.get("reversible") is False and tier == 0:
        verdict = "Block"
        reasons.append("BLOCK: irreversible action on a Tier-0 application")
    return GuardrailVerdict(proposal_id=proposal.proposal_id, verdict=verdict, failed_rules=failed,
                            reasons=reasons, passed_rules=passed, policy_version=version)


def evaluate_at(proposal: ActionProposal, when: str) -> GuardrailVerdict:
    """Evaluate as of a given sim timestamp (used by tests and what-if tools)."""
    return evaluate(proposal, parse(when))
