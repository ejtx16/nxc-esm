"""Gap fixes: a real alert cannot hide in a suppressed cluster; stale runbook articles get refreshed, and the
refresh unlocks safe auto-remediation (never inside the billing freeze, never for P1/P2)."""
from datetime import datetime

from app.data import db
from app.runtime import approvals, bus, engine
from app.runtime.clock import clock
from app.tools import runbooks
from tests.conftest import send_alert


def test_real_alert_joining_a_suppressed_cluster_brings_it_back(fresh):
    send_alert(system="DataWarehouse", name="Cache hit rate dropped below 40%", severity="Low", fp=True,
               recovered=True, ts="2025-08-04T09:00:00")
    assert db.one("SELECT status FROM clusters")["status"] == "suppressed"
    send_alert(system="DataWarehouse", name="Cache hit rate dropped below 40%", severity="High", fp=False,
               recovered=False, ts="2025-08-04T09:02:00")
    c = db.one("SELECT * FROM clusters")
    assert c["alert_count"] == 2 and c["status"] == "surfaced" and c["classification"] == "true_positive"


def test_a_second_recovering_duplicate_stays_suppressed(fresh):
    for ts in ("2025-08-04T09:00:00", "2025-08-04T09:02:00"):
        send_alert(system="DataWarehouse", name="Cache hit rate dropped below 40%", severity="Low", fp=True,
                   recovered=True, ts=ts)
    assert db.one("SELECT status FROM clusters")["status"] == "suppressed"


def test_runbook_library_covers_the_common_incident_types(fresh):
    assert len(runbooks.RUNBOOKS) >= 14
    for rid, rb in runbooks.RUNBOOKS.items():
        assert db.one("SELECT 1 FROM kb_articles WHERE article_id=?", (rb["kb_article"],)), f"{rid} has no KB article"
    assert runbooks.match("AuthService", "Session timeout occurring after 2 mins instead of 30 mins")["id"] == "RB-AU-02"
    assert runbooks.match("CustomerPortal", "FNOL submission portal down for mobile users")["id"] == "RB-CP-01"
    assert runbooks.match("WorkflowEngine", "Change management system not auto-closing resolved tasks")["id"] == "RB-WF-01"
    assert runbooks.match("LicenseCore", "Underwriting scoring module returning out-of-range values") is None


def _open_notification_incident() -> str:
    inc = "INC9001"
    db.insert("incidents", {"incident_id": inc, "short_description": "Push notification queue depth delayed",
                            "priority": "P3", "provisional_priority": "P3", "status": "Open", "origin": "scenario",
                            "app_id": "NotificationService", "reported_by": "test", "opened_ts": clock.now_iso(),
                            "reported_date": clock.now_iso()[:10], "sla_hours": 24, "escalated": 0, "held": 0})
    bus.publish(bus.INCIDENT_CREATED, inc, {})
    engine.process_pending()
    return inc


def test_stale_runbook_article_asks_for_a_refresh_and_approval_unlocks_the_fix(fresh):
    clock.reset(datetime(2025, 8, 12, 10, 0))                      # outside the month-end freeze
    inc = _open_notification_incident()
    assert db.one("SELECT status FROM incidents WHERE incident_id=?", (inc,))["status"] != "Resolved"
    pending = {r["action"]: r for r in db.query("SELECT * FROM approvals WHERE status='pending'")}
    assert "resolve" in pending and "update_kb_article" in pending           # held for the stale article, refresh drafted
    res = approvals.decide(pending["update_kb_article"]["approval_id"], True, "kb.manager")
    assert res["ok"] and inc in res["result"]["retriaged"]
    while engine.process_pending():
        pass
    assert db.one("SELECT status FROM incidents WHERE incident_id=?", (inc,))["status"] == "Resolved"
    assert db.one("SELECT status FROM approvals WHERE approval_id=?", (pending["resolve"]["approval_id"],))["status"] == "superseded"
    assert not db.scalar("SELECT COUNT(*) FROM approvals WHERE action='resolve' AND status='pending'")


def test_the_billing_freeze_still_blocks_self_healing_after_a_refresh(fresh):
    clock.reset(datetime(2025, 8, 4, 10, 0))                       # inside the freeze
    inc = _open_notification_incident()
    pend = db.one("SELECT approval_id FROM approvals WHERE action='update_kb_article' AND status='pending'")
    approvals.decide(pend["approval_id"], True, "kb.manager")
    while engine.process_pending():
        pass
    assert db.one("SELECT status FROM incidents WHERE incident_id=?", (inc,))["status"] != "Resolved"
    ap = db.one("SELECT failed_rules FROM approvals WHERE action='resolve' AND status='pending'")
    assert "respect_freeze_windows" in ap["failed_rules"]


def test_p1_is_not_re_triaged_by_a_kb_refresh(fresh):
    send_alert()                                                    # LicenseCore outage -> P1
    p = db.one("SELECT approval_id FROM approvals WHERE action='update_kb_article' AND status='pending'")
    assert p
    res = approvals.decide(p["approval_id"], True, "kb.manager")
    assert res["result"]["retriaged"] == []
    assert db.scalar("SELECT COUNT(*) FROM approvals WHERE action='resolve' AND status='pending'") == 1
