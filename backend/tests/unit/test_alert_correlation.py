from tests.conftest import make_alert, send_alert
from app.data import db
from app.runtime import engine, bus


def test_47_duplicates_collapse_to_one_cluster_and_incident(fresh):
    for i in range(47):
        send_alert(ts=f"2025-08-04T09:{i // 4:02d}:{(i % 4) * 15:02d}")
    cl = db.query("SELECT * FROM clusters")
    assert len(cl) == 1 and cl[0]["alert_count"] == 47
    assert cl[0]["status"] == "acknowledged" and cl[0]["incident_id"]
    assert db.scalar("SELECT COUNT(*) FROM incidents WHERE origin!='seed'") == 1


def test_dedupe_window_expires(fresh):
    send_alert(ts="2025-08-04T09:00:00")
    send_alert(ts="2025-08-04T09:09:00")        # inside 10 min window
    send_alert(ts="2025-08-04T09:30:00")        # 21 min after the last -> new cluster
    assert db.scalar("SELECT COUNT(*) FROM clusters") == 2


def test_dependent_system_alerts_correlate_into_the_incident(fresh):
    send_alert(system="LicenseCore", ts="2025-08-04T09:00:00")
    send_alert(system="BillingEngine", name="Queue depth > 10,000", severity="High", ts="2025-08-04T09:04:00")
    assert db.scalar("SELECT COUNT(*) FROM clusters") == 1
    c = db.one("SELECT * FROM clusters")
    assert "BillingEngine" in c["systems"] and c["alert_count"] == 2
    assert db.scalar("SELECT COUNT(*) FROM incidents WHERE origin!='seed'") == 1


def test_unrelated_system_does_not_join(fresh):
    send_alert(system="LicenseCore", ts="2025-08-04T09:00:00")
    send_alert(system="DataWarehouse", name="Disk usage > 85%", severity="Medium", ts="2025-08-04T09:01:00")
    assert db.scalar("SELECT COUNT(*) FROM clusters") == 2


def test_critical_true_positive_acknowledged_and_incident_opened_within_3_minutes(fresh):
    send_alert()
    c = db.one("SELECT * FROM clusters")
    assert c["ack_latency_s"] is not None and c["ack_latency_s"] < 180
    inc = db.one("SELECT * FROM incidents WHERE origin!='seed'")
    assert inc["source_cluster_id"] == c["cluster_id"] and inc["status"] != "Resolved"


def test_critical_tier0_gets_p1(fresh):
    send_alert()
    assert db.one("SELECT priority FROM incidents WHERE origin!='seed'")["priority"] == "P1"


def test_critical_tier3_gets_p2_not_p1(fresh):
    send_alert(system="NotificationService", name="Queue depth > 10,000")
    inc = db.one("SELECT * FROM incidents WHERE origin!='seed'")
    assert inc["provisional_priority"] == "P2"


def test_recovering_low_severity_noise_is_suppressed(fresh):
    send_alert(system="ReportingPlatform", name="Cache hit rate dropped below 40%", severity="Low", fp=True, recovered=True)
    c = db.one("SELECT * FROM clusters")
    assert c["status"] == "suppressed" and c["classification"] == "false_positive" and c["confidence"] >= 0.85
    assert db.scalar("SELECT COUNT(*) FROM incidents WHERE origin!='seed'") == 0


def test_critical_is_never_suppressed_even_if_it_looks_like_noise(fresh):
    send_alert(system="NotificationService", name="Memory usage spike", severity="Critical", fp=True, recovered=True)
    c = db.one("SELECT * FROM clusters")
    assert c["status"] != "suppressed"


def test_ambiguous_non_recovering_alert_is_surfaced_not_suppressed(fresh):
    send_alert(system="AuthService", name="Failed login attempts > 50/min", severity="High", fp=True, recovered=False)
    c = db.one("SELECT * FROM clusters")
    assert c["status"] == "surfaced"


def test_critical_joining_an_unacked_cluster_escalates_it(fresh):
    send_alert(system="DataWarehouse", name="Disk usage > 85%", severity="Medium", ts="2025-08-04T09:00:00")
    send_alert(system="DataWarehouse", name="Disk usage > 85%", severity="Critical", ts="2025-08-04T09:02:00")
    c = db.one("SELECT * FROM clusters")
    assert c["alert_count"] == 2 and c["status"] == "acknowledged" and c["incident_id"]


def test_every_alert_decision_is_audited(fresh):
    for i in range(5):
        send_alert(ts=f"2025-08-04T09:0{i}:00")
    assert db.scalar("SELECT COUNT(*) FROM agent_decisions WHERE agent='alert_correlation' AND kind='agent'") == 5


def test_kill_switch_makes_ack_and_incident_need_approval(fresh):
    db.set_setting("kill_switch", {"global": True, "agents": {}, "apps": {}})
    send_alert()
    assert db.scalar("SELECT COUNT(*) FROM incidents WHERE origin!='seed'") == 0
    pend = db.query("SELECT action FROM approvals WHERE status='pending' ORDER BY approval_id")
    assert [p["action"] for p in pend] == ["acknowledge", "open_incident"]
