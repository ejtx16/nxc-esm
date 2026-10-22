import csv

from app import settings
from app.agents import incident_triage as tri
from app.data import db
from app.runtime import approvals, bus, engine
from tests.conftest import send_alert


def test_golden_set_accuracy_meets_targets(fresh):
    rows = list(csv.DictReader(open(settings.GOLDEN_PATH)))
    assert len(rows) >= 15
    cat = within = 0
    for r in rows:
        d = tri.decide(r["description"], allow_llm=False)
        cat += d["category"] == r["category"]
        within += abs(int(d["priority"][1]) - int(r["priority"][1])) <= 1
    assert cat / len(rows) >= 0.80, f"category accuracy {cat}/{len(rows)}"
    assert within / len(rows) >= 0.90, f"priority within +/-1 {within}/{len(rows)}"


def test_routing_uses_skills_not_round_robin(fresh):
    team, _ = tri.pick_team("Access Issue", "AuthService", "P2")
    assert team == "Security Ops"
    team, _ = tri.pick_team("Performance Degradation", "ReportingPlatform", "P3")
    assert team == "Database Team"


def test_tie_broken_by_lowest_open_workload(fresh):
    # Make Infra Team and App Support L2 score equally for an unmapped app, then load one of them.
    base, _ = tri.pick_team("Integration Failure", None, "P2")
    for i in range(30):
        db.insert("incidents", {"incident_id": f"INCX{i}", "assigned_team": base, "status": "Open", "origin": "scenario",
                                "priority": "P3", "short_description": "x"})
    after, _ = tri.pick_team("Integration Failure", None, "P2")
    assert after != base


def test_triage_decision_has_rationale_similar_incidents_and_confidence(fresh):
    send_alert()
    inc = db.one("SELECT * FROM incidents WHERE origin!='seed'")
    assert inc["category"] and inc["priority"] == "P1" and inc["assigned_team"] == "App Support L2"
    assert inc["rationale"] and 0 < inc["confidence"] <= 1
    assert inc["escalated"] == 1                                       # P1 escalated immediately


def test_similar_incidents_are_ranked_by_text_similarity(fresh):
    sims = tri.similar_incidents("SSO login failing after certificate renewal", 3)
    assert sims and "SSO" in sims[0]["short_description"]
    assert len({s["short_description"] for s in sims}) == len(sims)


def test_kb_below_threshold_not_attached_and_reason_given(fresh):
    send_alert()
    inc = db.one("SELECT * FROM incidents WHERE origin!='seed'")
    assert "KB4022" not in (inc["kb_article_ids"] or "")
    assert "excluded" in inc["rationale"]


def test_sla_check_escalates_p2_at_50_percent(fresh):
    from app.runtime.clock import clock
    from app.tools import mock_servicenow as snow
    db.insert("incidents", {"incident_id": "INC2900", "priority": "P2", "category": "Application Error", "status": "Assigned",
                            "short_description": "Payment gateway down", "origin": "scenario", "sla_hours": 8,
                            "opened_ts": clock.now_iso(), "assigned_team": "App Support L2", "app_id": "BillingEngine",
                            "escalated": 0})
    clock.advance(3.9 * 3600)
    assert engine.sla_scan() == 0
    clock.advance(0.2 * 3600)                                         # 4.1h > 50% of 8h
    assert engine.sla_scan() == 1
    engine.process_pending()
    inc = db.one("SELECT * FROM incidents WHERE incident_id='INC2900'")
    assert inc["escalated"] == 1 and inc["breach_risk"] >= 0.5


def test_p1_triggers_cross_domain_surge_fanout(fresh):
    send_alert()
    types = [e["type"] for e in db.query("SELECT type FROM events ORDER BY event_id")]
    assert {"sr.surge_mode_on", "kb.validate_related", "triage.hold_reassignments"} <= set(types)
    assert db.get_setting("surge_mode") is True and db.get_setting("triage_hold") is True


def test_p3_reassignment_held_during_surge_then_released(fresh):
    send_alert()
    db.insert("incidents", {"incident_id": "INC2950", "priority": "P3", "status": "Open", "origin": "scenario",
                            "short_description": "Mobile app push notifications delayed by up to 6 hours",
                            "reported_date": "2025-08-04", "opened_ts": "2025-08-04T09:05:00", "sla_hours": 24})
    bus.publish(bus.INCIDENT_CREATED, "INC2950", {})
    engine.process_pending()
    held = db.one("SELECT * FROM incidents WHERE incident_id='INC2950'")
    assert held["held"] == 1 and held["status"] == "Held" and held["assigned_team"] is None
    # resolve the P1 -> surge clears -> held incident is re-triaged and routed
    ap = db.one("SELECT approval_id FROM approvals WHERE action='resolve' AND entity_ref='INC2001'")
    assert approvals.decide(ap["approval_id"], True, "ops")["ok"]
    engine.process_pending()
    assert db.get_setting("surge_mode") is False
    released = db.one("SELECT * FROM incidents WHERE incident_id='INC2950'")
    assert released["held"] == 0 and released["assigned_team"]
