"""docs/validation/data/alert-log.json replay (rules mode): 3 duplicate noise alerts are suppressed, 1 real Critical becomes a P1."""
import pytest

from app.data import db
from app.runtime import player


@pytest.fixture
def replayed(fresh):
    return player.run_sync("json_alerts")


def test_noise_duplicates_are_deduped_and_suppressed(replayed):
    assert db.scalar("SELECT COUNT(*) FROM alerts WHERE run_id=?", (replayed,)) == 4
    cl = db.one("SELECT * FROM clusters WHERE run_id=? AND primary_system='ReportingPlatform'", (replayed,))
    assert (cl["alert_count"], cl["status"], cl["incident_id"]) == (3, "suppressed", None)


def test_real_alert_becomes_p1_with_a_person(replayed):
    (inc,) = db.query("SELECT * FROM incidents WHERE run_id=?", (replayed,))
    assert (inc["app_id"], inc["priority"]) == ("LicenseCore", "P1")
    assert inc["status"] != "Resolved" and inc["assigned_agent"].startswith("On-call engineer")
    ap = db.one("SELECT * FROM approvals WHERE action='resolve' AND entity_ref=?", (inc["incident_id"],))
    assert ap["status"] == "pending" and "max_app_tier_blocked" in db.loads(ap["failed_rules"])


def test_noise_kpi_for_the_run(replayed):
    from app.api.routes import core
    ov = core.overview()
    assert ov["alerts"] == {**ov["alerts"], "raw": 4, "clusters": 2, "suppressed_clusters": 1, "noise_alerts": 3,
                            "classified_correctly": 4, "real_suppressed": 0, "raw_noise_pct": 75.0,
                            "surfaced_noise_pct": 0.0}
    assert (ov["incidents"]["auto_resolved"], ov["incidents"]["with_person"]) == (0, 1)


def test_kpi_page_shows_the_run(replayed):
    from app.api.routes import sim
    k = {x["key"]: x for x in sim.kpis_run()["kpis"]}
    assert k["alert_noise"]["after"] == 0.0 and k["alert_noise"]["target_met"]
    assert k["alert_noise"]["detail"]["classified_correctly"] == 4
    assert {"ack_time", "triage_time"} <= set(k) and "self_service" not in k


def test_unsure_noise_is_suppressed_once_repeats_clear_on_their_own(fresh, monkeypatch):
    # Live mode can rate the first alert below the suppress bar (Claude said 0.80); the repeats must finish the job.
    from app.agents import alert_correlation as ac
    real = ac._assess
    monkeypatch.setattr(ac, "_assess", lambda alert, cid, p, ev: {**real(alert, cid, p, ev), "confidence": 0.8}
                        if alert["source_system"] == "ReportingPlatform" else real(alert, cid, p, ev))
    rid = player.run_sync("json_alerts")
    cl = db.one("SELECT * FROM clusters WHERE run_id=? AND primary_system='ReportingPlatform'", (rid,))
    assert (cl["status"], cl["confidence"]) == ("suppressed", 0.9)
