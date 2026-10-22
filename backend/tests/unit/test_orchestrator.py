from app.data import audit, db
from app.graph.state import ActionProposal
from app.runtime import approvals, bus, engine
from tests.conftest import send_alert


def test_audit_log_is_append_only(fresh):
    send_alert()
    did = db.scalar("SELECT MIN(decision_id) FROM agent_decisions")
    for sql in ("UPDATE agent_decisions SET reasoning='tampered' WHERE decision_id=%d" % did,
                "DELETE FROM agent_decisions WHERE decision_id=%d" % did):
        try:
            db.execute(sql)
            raise AssertionError("audit log was modified")
        except Exception as exc:  # noqa: BLE001
            assert "append-only" in str(exc)


def test_decision_chain_agent_guardrail_execution_human(fresh):
    send_alert()
    ap = db.one("SELECT * FROM approvals WHERE action='resolve'")
    approvals.decide(ap["approval_id"], True, "ops", note="approved during bridge")
    chain = db.query("SELECT kind, agent, guardrail_verdict, human_decision, parent_id, decision_id FROM agent_decisions "
                     "WHERE entity_ref='INC2001' ORDER BY decision_id")
    kinds = [c["kind"] for c in chain]
    assert kinds[0] == "agent" and "guardrail" in kinds and "human" in kinds and kinds[-1] == "execution"
    g = [c for c in chain if c["kind"] == "guardrail" and c["guardrail_verdict"] == "RequireApproval"]
    assert g and all(c["parent_id"] for c in g)
    assert [c for c in chain if c["kind"] == "human"][0]["human_decision"] == "approved"


def test_every_verdict_including_approvals_is_audited(fresh):
    send_alert()
    approved = db.scalar("SELECT COUNT(*) FROM agent_decisions WHERE kind='guardrail' AND guardrail_verdict='Approve'")
    assert approved >= 4


def test_agent_exception_falls_back_to_manual_queue_and_flow_continues(fresh, monkeypatch):
    from app.agents import alert_correlation

    def explode(state):
        raise RuntimeError("boom")
    monkeypatch.setattr(alert_correlation, "run", explode)
    import app.graph.nodes as nodes
    from app.graph.build import build_graph
    monkeypatch.setitem(nodes.NODE_FUNCS, "alert_correlation", nodes.safe("alert_correlation", explode))
    engine._graph = build_graph()
    try:
        send_alert()
        ap = db.one("SELECT * FROM approvals WHERE action='manual_review'")
        assert ap and ap["reason_code"] == "agent_failure" and "boom" in ap["reasons"]
        assert db.one("SELECT status FROM events WHERE type='alert.received'")["status"] == "done"
        send_alert(ts="2025-08-04T09:01:00")                               # next event still processed
        assert db.scalar("SELECT COUNT(*) FROM approvals WHERE action='manual_review'") == 2
    finally:
        engine._graph = None


def test_orchestrator_level_failure_creates_manual_item(fresh, monkeypatch):
    class Broken:
        def invoke(self, state):
            raise ValueError("graph exploded")
    monkeypatch.setattr(engine, "_graph", Broken())
    bus.publish(bus.KB_REVIEW, "KB4001", {})
    engine.process_pending()
    ev = db.one("SELECT * FROM events ORDER BY event_id DESC LIMIT 1")
    assert ev["status"] == "failed" and "graph exploded" in ev["error"]
    ap = db.one("SELECT * FROM approvals ORDER BY approval_id DESC LIMIT 1")
    assert ap["action"] == "manual_review" and ap["reason_code"] == "agent_failure"
    engine._graph = None


def test_approving_a_blocked_item_is_refused(fresh):
    db.execute("""INSERT INTO approvals (action, agent, entity_ref, summary, proposal, verdict, status, created_ts)
                  VALUES ('resolve','incident_triage','INC1','x','{}','Block','blocked','t')""")
    res = approvals.decide(1, True)
    assert not res["ok"] and res["status"] == 409
    assert approvals.decide(1, False)["ok"]


def test_double_decision_is_rejected(fresh):
    send_alert()
    ap = db.one("SELECT approval_id FROM approvals WHERE action='resolve'")
    assert approvals.decide(ap["approval_id"], True)["ok"]
    assert approvals.decide(ap["approval_id"], True)["status"] == 409


def test_rejecting_a_suppression_resurfaces_the_cluster(fresh):
    db.set_setting("kill_switch", {"global": True, "agents": {}, "apps": {}})
    send_alert(system="ReportingPlatform", name="Cache hit rate dropped below 40%", severity="Low", fp=True, recovered=True)
    ap = db.one("SELECT approval_id FROM approvals WHERE action='suppress'")
    approvals.decide(ap["approval_id"], False, "ops")
    assert db.one("SELECT status FROM clusters")["status"] == "surfaced"


def test_approving_after_kill_switch_executes_the_held_action(fresh):
    db.set_setting("kill_switch", {"global": True, "agents": {}, "apps": {}})
    send_alert()
    assert db.scalar("SELECT COUNT(*) FROM incidents WHERE origin!='seed'") == 0
    for ap in db.query("SELECT approval_id FROM approvals WHERE status='pending' ORDER BY approval_id"):
        approvals.decide(ap["approval_id"], True, "ops")
    engine.process_pending()
    assert db.scalar("SELECT COUNT(*) FROM incidents WHERE origin!='seed'") == 1


def test_resolving_p1_updates_incident_and_records_outcome(fresh):
    send_alert()
    ap = db.one("SELECT approval_id FROM approvals WHERE action='resolve'")
    approvals.decide(ap["approval_id"], True, "ops")
    inc = db.one("SELECT * FROM incidents WHERE incident_id='INC2001'")
    assert inc["status"] == "Resolved" and "RB-LC-01" in inc["resolution_notes"]
    assert db.scalar("SELECT COUNT(*) FROM action_outcomes WHERE action='resolve'") == 1


def test_low_risk_incident_is_auto_resolved_unattended_when_kb_is_trusted(fresh, monkeypatch):
    """P3, Tier 3, reversible runbook, KB >= 90%, small blast radius, outside freeze -> Guardrail approves."""
    from app.runtime.clock import clock
    from datetime import datetime
    clock.reset(datetime(2025, 8, 12, 10, 0))
    db.update("kb_articles", "article_id", "KB4016", {"score": 96.0, "recommendation": "keep"})
    db.insert("incidents", {"incident_id": "INC2800", "priority": "P3", "status": "Open", "origin": "scenario",
                            "short_description": "Mobile app push notifications delayed by up to 6 hours",
                            "reported_date": "2025-08-12", "opened_ts": "2025-08-12T10:00:00", "sla_hours": 24,
                            "app_id": "NotificationService"})
    bus.publish(bus.INCIDENT_CREATED, "INC2800", {})
    engine.process_pending()
    inc = db.one("SELECT * FROM incidents WHERE incident_id='INC2800'")
    assert inc["status"] == "Resolved", inc
    assert "RB-NS-01" in inc["resolution_notes"]
    assert db.scalar("SELECT COUNT(*) FROM approvals WHERE entity_ref='INC2800' AND action='resolve'") == 0


def test_retriage_does_not_stack_duplicate_pending_approvals(fresh):
    send_alert()
    first = db.scalar("SELECT COUNT(*) FROM approvals WHERE action='resolve' AND entity_ref='INC2001' AND status='pending'")
    assert first == 1
    for _ in range(3):
        bus.publish(bus.INCIDENT_PRIORITY_CHANGED, "INC2001", {})
        engine.process_pending()
    assert db.scalar("SELECT COUNT(*) FROM approvals WHERE action='resolve' AND entity_ref='INC2001' AND status='pending'") == 1


def test_a_new_proposal_is_allowed_once_the_earlier_one_is_decided(fresh):
    send_alert()
    ap = db.one("SELECT approval_id FROM approvals WHERE action='resolve' AND entity_ref='INC2001'")
    approvals.decide(ap["approval_id"], False, "ops")
    bus.publish(bus.INCIDENT_PRIORITY_CHANGED, "INC2001", {})
    engine.process_pending()
    assert db.scalar("SELECT COUNT(*) FROM approvals WHERE action='resolve' AND entity_ref='INC2001' AND status='pending'") == 1
