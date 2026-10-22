"""Full licensecore_outage replay in LLM_MODE=rules produces the expected incidents, verdicts and audit rows."""
import pytest

from app.data import db, scenarios
from app.runtime import approvals, engine, player


@pytest.fixture
def replayed(fresh):
    rid = player.run_sync("licensecore_outage")
    return rid


def test_47_alerts_become_one_incident(replayed):
    c = db.one("SELECT * FROM clusters WHERE primary_system='LicenseCore'")
    assert c["alert_count"] == 53                        # 47 LicenseCore duplicates + 6 correlated BillingEngine alerts
    lc = db.scalar("SELECT COUNT(*) FROM alerts WHERE cluster_id=? AND source_system='LicenseCore'", (c["cluster_id"],))
    be = db.scalar("SELECT COUNT(*) FROM alerts WHERE cluster_id=? AND source_system='BillingEngine'", (c["cluster_id"],))
    assert (lc, be) == (47, 6)
    assert db.scalar("SELECT COUNT(DISTINCT incident_id) FROM alerts WHERE cluster_id=?", (c["cluster_id"],)) == 1
    assert c["status"] == "acknowledged" and c["ack_latency_s"] < 180


def test_only_one_p1_exists_and_background_noise_does_not_open_incidents(replayed):
    inc = db.query("SELECT * FROM incidents WHERE origin!='seed' AND source_cluster_id IS NOT NULL")
    assert [i["priority"] for i in inc] == ["P1"]


def test_noise_is_cut_and_no_critical_alert_is_suppressed(replayed):
    clusters = db.query("SELECT * FROM clusters")
    assert sum(c["status"] == "suppressed" for c in clusters) >= 10
    assert not [c for c in clusters if c["status"] == "suppressed" and c["max_severity"] == "Critical"]
    raw = db.scalar("SELECT COUNT(*) FROM alerts WHERE run_id=?", (replayed,))
    assert raw > 150 and len(clusters) < raw / 3


def test_p1_is_triaged_with_explanation_and_escalated(replayed):
    i = db.one("SELECT * FROM incidents WHERE priority='P1' AND origin!='seed'")
    assert i["assigned_team"] == "App Support L2" and i["escalated"] == 1 and i["category"]
    assert "Tier 0" in i["rationale"] and i["confidence"] > 0


def test_guardrail_blocks_p1_auto_resolution_with_reasons(replayed):
    ap = db.one("SELECT * FROM approvals WHERE action='resolve' AND entity_ref='INC2001'")
    assert ap["status"] == "pending" and ap["verdict"] == "RequireApproval"
    failed = set(db.loads(ap["failed_rules"]))
    assert {"allowed_priorities", "max_app_tier_blocked", "respect_freeze_windows", "min_kb_accuracy"} <= failed
    assert db.one("SELECT status FROM incidents WHERE incident_id='INC2001'")["status"] != "Resolved"


def test_surge_mode_drains_the_sr_queue_and_keeps_resets_flowing(replayed):
    assert db.get_setting("surge_mode") is True
    assert db.scalar("SELECT COUNT(*) FROM service_requests WHERE origin!='seed' AND status='Pending Human'") == 0
    done = db.scalar("SELECT COUNT(*) FROM service_requests WHERE origin!='seed' AND status='Completed' "
                     "AND fulfilled_by='agent:sr_fulfilment'")
    assert done >= 20
    priv = db.one("SELECT * FROM service_requests WHERE target_system='BillingEngine' AND access_level='admin'")
    assert priv["status"] == "Pending Approval"


def test_kb_agent_validated_related_articles_and_drafted_a_fix(replayed):
    d = db.one("SELECT * FROM kb_drafts")
    assert d and d["article_id"] == "KB4022" and d["status"] == "pending"
    decisions = db.query("SELECT * FROM agent_decisions WHERE agent='kb_curation' AND input_summary LIKE 'kb.validate_related%'")
    assert decisions


def test_p3_tickets_were_held_during_the_surge(replayed):
    held = db.query("SELECT * FROM incidents WHERE origin='scenario' AND source_cluster_id IS NULL")
    assert held and all(h["held"] == 1 for h in held if h["priority"] in ("P3", "P4"))


def test_human_approval_resolves_p1_and_ends_the_surge(replayed):
    ap = db.one("SELECT approval_id FROM approvals WHERE action='resolve' AND entity_ref='INC2001'")
    assert approvals.decide(ap["approval_id"], True, "svc.delivery.mgr")["ok"]
    engine.process_pending()
    assert db.one("SELECT status FROM incidents WHERE incident_id='INC2001'")["status"] == "Resolved"
    assert db.get_setting("surge_mode") is False and db.get_setting("triage_hold") is False
    assert db.scalar("SELECT COUNT(*) FROM incidents WHERE held=1") == 0


def test_every_event_processed_without_failures_and_audit_is_complete(replayed):
    assert db.scalar("SELECT COUNT(*) FROM events WHERE status NOT IN ('done')") == 0
    assert db.scalar("SELECT COUNT(*) FROM agent_decisions WHERE kind='agent' AND agent='alert_correlation'") == \
        db.scalar("SELECT COUNT(*) FROM alerts WHERE run_id=?", (replayed,))
    # GR-1: every proposal any agent made was intercepted by exactly one guardrail verdict.
    proposed = sum(len(db.loads(r["proposed_action"], [])) for r in
                   db.query("SELECT proposed_action FROM agent_decisions WHERE kind='agent' AND proposed_action IS NOT NULL"))
    assert proposed > 50
    assert db.scalar("SELECT COUNT(*) FROM agent_decisions WHERE kind='guardrail'") == proposed


def test_scenarios_are_deterministic_for_a_seed(fresh):
    a, b = scenarios.build("licensecore_outage", 7), scenarios.build("licensecore_outage", 7)
    assert [(e.t, e.kind, e.data) for e in a.events] == [(e.t, e.kind, e.data) for e in b.events]
    c = scenarios.build("licensecore_outage", 8)
    assert [(e.t, e.data) for e in a.events] != [(e.t, e.data) for e in c.events]


def test_scenario_shapes(fresh):
    out = scenarios.build("licensecore_outage")
    lc = [e for e in out.events if e.kind == "alert" and e.data["system"] == "LicenseCore"]
    assert len(lc) == 47 and out.duration_s == 1320 and out.default_speed * 60 == pytest.approx(1320)
    sr = scenarios.build("sr_surge")
    assert sum(e.kind == "sr" for e in sr.events) == 3200
    types = [e.data["request_type"] for e in sr.events if e.kind == "sr"]
    assert 0.90 <= sum(t in ("Password Reset", "Access Provisioning") for t in types) / 3200 <= 0.97       # ~94% per the case


def test_alert_agent_checks_kb_and_hands_off_to_triage(replayed):
    a = db.one("SELECT reasoning FROM agent_decisions WHERE agent='alert_correlation' AND reasoning LIKE '%Handing %'")
    assert a and "knowledge base" in a["reasoning"] and "Incident Triage agent" in a["reasoning"]
    assert db.one("SELECT 1 FROM agent_decisions WHERE agent='incident_triage' AND reasoning LIKE 'Picked up %'")


def test_resolution_closes_the_alert_ticket_and_sends_a_notice(replayed):
    ap = db.one("SELECT approval_id FROM approvals WHERE action='resolve' AND entity_ref='INC2001'")
    assert approvals.decide(ap["approval_id"], True, "svc.delivery.mgr")["ok"]
    row = db.one("SELECT reasoning FROM agent_decisions WHERE kind='execution' AND reasoning LIKE '% is resolved.%'")
    assert row and "Closed alert group" in row["reasoning"] and "resolution notice" in row["reasoning"]
    inc = db.one("SELECT * FROM incidents WHERE status='Resolved' AND source_cluster_id IS NOT NULL")
    assert db.one("SELECT status FROM clusters WHERE cluster_id=?", (inc["source_cluster_id"],))["status"] == "resolved"
    assert not db.one("SELECT 1 FROM alerts WHERE cluster_id=? AND status!='resolved'", (inc["source_cluster_id"],))
    assert db.one("SELECT 1 FROM audit_spans WHERE name='mock_servicenow.notify_resolved' AND entity_ref=?",
                  (inc["incident_id"],))
