"""Simulation 2: the self_service_requests scenario replayed in LLM_MODE=rules.

Two portal requests: the Guardrail classes the password reset as low risk and the agent completes it from the
knowledge document; the admin-access request is high risk and waits for a human approver. Both are audited."""
import pytest

from app.data import db, scenarios
from app.runtime import approvals, engine, player


@pytest.fixture
def replayed(fresh):
    return player.run_sync("self_service_requests")


def _srs(run_id: str) -> dict[str, dict]:
    rows = db.query("SELECT * FROM service_requests WHERE run_id=? ORDER BY sr_id", (run_id,))
    for r in rows:
        r["lines"] = [t["text"] for t in db.loads(r["trace"], [])]
    return {r["request_type"]: r for r in rows}


def test_exactly_two_portal_requests_arrive(replayed):
    srs = _srs(replayed)
    assert set(srs) == {"Password Reset", "Access Provisioning"}
    assert all(r["channel"] == "portal" and r["origin"] == "scenario" for r in srs.values())
    assert db.scalar("SELECT COUNT(*) FROM events WHERE run_id=? AND status!='done'", (replayed,)) == 0


def test_low_risk_reset_is_completed_from_the_knowledge_document(replayed):
    r = _srs(replayed)["Password Reset"]
    assert r["status"] == "Completed" and r["fulfilled_by"] == "agent:sr_fulfilment" and r["sla_met"] == "Yes"
    kb = next(l for l in r["lines"] if l.startswith("Knowledge: "))
    assert kb.startswith("Knowledge: KB4001 'How to reset user password via admin console'")
    assert any(l.startswith("Guardrail: low risk - approved to run without a person") for l in r["lines"])
    followed = next(l for l in r["lines"] if l.startswith("Followed the solution in KB4001"))
    assert "temporary credential" in followed
    assert r["outcome"] == "password reset (per KB4001)"
    assert db.scalar("SELECT COUNT(*) FROM iam_log WHERE action='reset_password' AND employee_id='EMP1002'") == 1
    assert db.scalar("SELECT COUNT(*) FROM approvals WHERE entity_ref=?", (r["sr_id"],)) == 0


def test_high_risk_admin_access_waits_for_a_person(replayed):
    r = _srs(replayed)["Access Provisioning"]
    assert r["status"] == "Pending Approval" and r["target_system"] == "BillingEngine" and r["access_level"] == "admin"
    assert any(l.startswith("Guardrail: high risk - needs a human approver") for l in r["lines"])
    ap = db.one("SELECT * FROM approvals WHERE entity_ref=? AND action='grant_access'", (r["sr_id"],))
    assert ap["status"] == "pending" and ap["verdict"] == "RequireApproval" and ap["run_id"] == replayed
    assert "block_privileged_access" in db.loads(ap["failed_rules"])
    assert db.scalar("SELECT COUNT(*) FROM iam_log WHERE action='grant_access'") == 0


def test_knowledge_document_is_a_tool_span_and_in_the_guardrail_context(replayed):
    srs = _srs(replayed)
    for r in srs.values():
        assert db.one("SELECT 1 FROM audit_spans WHERE run_id=? AND entity_ref=? AND name='kb_store.solution_for'",
                      (replayed, r["sr_id"]))
    ap = db.one("SELECT context FROM approvals WHERE entity_ref=?", (srs["Access Provisioning"]["sr_id"],))
    ctx = db.loads(ap["context"])
    assert ctx["kb_article"] == "KB4015" and ctx["privileged"] is True


def test_guardrail_rows_carry_the_risk_class(replayed):
    srs = _srs(replayed)
    low = db.one("SELECT * FROM agent_decisions WHERE kind='guardrail' AND entity_ref=?", (srs["Password Reset"]["sr_id"],))
    high = db.one("SELECT * FROM agent_decisions WHERE kind='guardrail' AND entity_ref=?", (srs["Access Provisioning"]["sr_id"],))
    assert low["guardrail_verdict"] == "Approve" and low["reasoning"].startswith("low risk:")
    assert db.loads(low["guardrail_reason"])["risk"] == "low"
    assert high["guardrail_verdict"] == "RequireApproval" and high["reasoning"].startswith("high risk:")
    assert db.loads(high["guardrail_reason"])["risk"] == "high"


def test_both_requests_are_in_the_audit_trail_for_the_run(replayed):
    for r in _srs(replayed).values():
        chain = db.query("SELECT kind FROM agent_decisions WHERE entity_ref=? AND run_id=? ORDER BY decision_id",
                         (r["sr_id"], replayed))
        kinds = [c["kind"] for c in chain]
        assert kinds[:2] == ["agent", "guardrail"]
        assert kinds[2] == ("execution" if r["status"] == "Completed" else "queued")
        assert db.scalar("SELECT COUNT(DISTINCT trace_id) FROM audit_spans WHERE entity_ref=? AND run_id=?",
                         (r["sr_id"], replayed)) == 1


def test_approving_the_held_request_grants_access_and_closes_the_loop(replayed):
    r = _srs(replayed)["Access Provisioning"]
    ap = db.one("SELECT approval_id FROM approvals WHERE entity_ref=?", (r["sr_id"],))
    assert approvals.decide(ap["approval_id"], True, "svc.delivery.mgr")["ok"]
    engine.process_pending()
    after = db.one("SELECT * FROM service_requests WHERE sr_id=?", (r["sr_id"],))
    assert after["status"] == "Completed" and after["fulfilled_by"] == "approved:svc.delivery.mgr"
    assert after["outcome"] == "admin access to BillingEngine (per KB4015)"
    assert db.scalar("SELECT COUNT(*) FROM iam_log WHERE action='grant_access' AND system='BillingEngine'") == 1
    assert db.one("SELECT kind FROM agent_decisions WHERE entity_ref=? ORDER BY decision_id DESC LIMIT 1",
                  (r["sr_id"],))["kind"] == "execution"


def test_scenario_shape(fresh):
    sc = scenarios.build("self_service_requests")
    assert sc.total == 2 and sc.duration_s == 60 and sc.default_speed == 1.0
    assert [e.kind for e in sc.events] == ["sr", "sr"] and all(e.data["channel"] == "portal" for e in sc.events)
    assert any(s["name"] == "self_service_requests" for s in scenarios.catalogue())
