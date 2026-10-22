"""REST API exercised through FastAPI's TestClient with the real background worker."""
import time

import pytest
from fastapi.testclient import TestClient

from app.api.main import app
from app.data import db


@pytest.fixture
def api(fresh):
    with TestClient(app) as c:
        yield c


def wait_for(fn, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


def test_health_and_overview(api):
    h = api.get("/api/health").json()
    assert h["ok"] and h["model"] == "claude-opus-5-5" and h["llm_mode"] == "rules"
    o = api.get("/api/overview").json()
    assert o["kb"]["articles"] == 30 and o["surge_mode"] is False and o["approvals"]["pending"] == 0
    assert o["incidents"]["resolved"] == 0 and o["incidents"]["auto_resolved"] == 0


def test_live_scenario_end_to_end_through_the_api(api):
    r = api.post("/api/scenarios/licensecore_outage/start", json={"speed": 400})
    assert r.status_code == 200 and r.json()["active"]
    assert api.post("/api/scenarios/licensecore_outage/start", json={}).status_code == 409        # one at a time
    wait_for(lambda: api.get("/api/scenarios/status").json()["active"] is False, 30)
    wait_for(lambda: api.get("/api/overview").json()["pending_events"] == 0, 20)
    inc = api.get("/api/incidents", params={"status": "open"}).json()
    p1 = next(i for i in inc if i["priority"] == "P1")
    assert p1["assigned_team"] == "App Support L2" and p1["tier"] == 0 and p1["sla"]["window_h"] == 1
    detail = api.get(f"/api/incidents/{p1['incident_id']}").json()
    assert detail["cluster"]["alert_count"] == 53 and detail["decisions"] and detail["kb_excluded"]
    q = api.get("/api/approvals", params={"status": "open"}).json()
    resolve = next(a for a in q if a["action"] == "resolve" and a["entity_ref"] == p1["incident_id"])
    assert "allowed_priorities" in resolve["failed_rules"] and resolve["reasons"]
    res = api.post(f"/api/approvals/{resolve['approval_id']}/decide", json={"approve": True, "decided_by": "api.test"})
    assert res.status_code == 200 and res.json()["state"] == "executed"
    assert api.post(f"/api/approvals/{resolve['approval_id']}/decide", json={"approve": True}).status_code == 409
    wait_for(lambda: api.get("/api/overview").json()["surge_mode"] is False, 10)
    o = api.get("/api/overview").json()["incidents"]
    assert o["resolved"] >= 1 and o["auto_resolved"] == o["resolved"] - 1      # the P1 fix had a person approve it
    sla = api.get("/api/overview").json()["sla"]
    assert sla["incidents_closed"] >= 1 and 0 <= sla["met_pct"] <= 100
    live = api.get("/api/live-feed").json()
    assert live["raw_alerts"] > 150 and live["cluster_count"] < 60


def test_self_service_password_reset_and_privileged_request(api):
    r = api.post("/api/self-service", json={"employee_id": "EMP1002", "request_type": "Password Reset"}).json()
    assert r["sr"]["status"] == "Completed" and r["sr"]["fulfilled_by"] == "agent:sr_fulfilment"
    assert any("Password reset issued" in t["text"] for t in r["sr"]["trace"])
    r = api.post("/api/self-service", json={"employee_id": "EMP1003", "request_type": "Access Provisioning",
                                            "target_system": "BillingEngine", "access_level": "admin"}).json()
    assert r["sr"]["status"] == "Pending Approval" and r["approvals"][0]["verdict"] == "RequireApproval"
    assert api.post("/api/self-service", json={"employee_id": "", "request_type": "Password Reset"}).status_code == 400
    assert api.post("/api/self-service", json={"employee_id": "EMP1002"}).status_code == 400
    bad = api.post("/api/self-service", json={"employee_id": "EMP9999", "request_type": "Password Reset"}).json()
    assert bad["sr"]["status"] == "Rejected"


def test_kill_switch_via_api_forces_approval(api):
    assert api.post("/api/settings/kill-switch", json={"global": True}).json()["global"] is True
    r = api.post("/api/self-service", json={"employee_id": "EMP1002", "request_type": "Password Reset"}).json()
    assert r["sr"]["status"] == "Pending Approval" and r["approvals"][0]["failed_rules"] == ["kill_switch"]
    api.post("/api/settings/kill-switch", json={"global": False})
    r = api.post("/api/self-service", json={"employee_id": "EMP1002", "request_type": "Password Reset"}).json()
    assert r["sr"]["status"] == "Completed"


def test_kb_endpoints_draft_diff_and_approve(api):
    arts = api.get("/api/kb/articles").json()
    assert len(arts) == 30 and arts[0]["score"] <= arts[-1]["score"]
    assert api.post("/api/kb/articles/KB4022/draft").json()["queued"]
    detail = wait_for(lambda: (d := api.get("/api/kb/articles/KB4022").json())["drafts"] and d)
    d = detail["drafts"][0]
    assert d["status"] == "pending" and any(l["op"] == "+" for l in d["diff"]) and any(l["op"] == "-" for l in d["diff"])
    ap = wait_for(lambda: api.get("/api/kb/articles/KB4022").json()["drafts"][0]["approval"])
    assert api.post(f"/api/approvals/{ap['approval_id']}/decide", json={"approve": True, "decided_by": "km"}).json()["state"] == "executed"
    after = api.get("/api/kb/articles/KB4022").json()
    assert after["score"] >= 90 and after["updated_by_agent"] == 1
    assert api.get("/api/kb/articles/NOPE").status_code == 404


def test_audit_feed_and_entity_chain(api):
    api.post("/api/self-service", json={"employee_id": "EMP1002", "request_type": "Password Reset"})
    feed = api.get("/api/feed", params={"limit": 20}).json()
    assert feed and feed[0]["decision_id"] > feed[-1]["decision_id"]
    sid = next(f["entity_ref"] for f in feed if f["entity_ref"].startswith("SR"))
    chain = api.get(f"/api/decisions/{sid}").json()
    assert [c["kind"] for c in chain][:2] == ["agent", "guardrail"]
    newer = api.get("/api/feed", params={"since_id": feed[0]["decision_id"]}).json()
    assert newer == []


def test_sim_advance_escalates_p1_before_breach(api):
    api.post("/api/scenarios/licensecore_outage/start", json={"speed": 800})
    wait_for(lambda: api.get("/api/scenarios/status").json()["active"] is False, 30)
    db_inc = db.one("SELECT * FROM incidents WHERE priority='P1' AND origin!='seed'")
    # approve+reject are not needed: the P1 is already escalated at creation; use a P2 ticket for the SLA path
    db.insert("incidents", {"incident_id": "INC2999", "priority": "P2", "status": "Assigned", "origin": "scenario",
                            "short_description": "Payment gateway returning duplicate transaction references", "sla_hours": 8,
                            "opened_ts": api.get("/api/health").json()["sim_now"], "assigned_team": "App Support L2",
                            "app_id": "BillingEngine", "escalated": 0})
    r = api.post("/api/sim/advance", json={"minutes": 5 * 60}).json()
    assert r["sla_checks_queued"] >= 1
    wait_for(lambda: db.one("SELECT escalated FROM incidents WHERE incident_id='INC2999'")["escalated"] == 1)


def test_self_service_scenario_through_the_api_clears_and_refills_the_audit_trail(api):
    api.post("/api/self-service", json={"employee_id": "EMP1004", "request_type": "Password Reset"})   # an earlier trace
    assert db.scalar("SELECT COUNT(*) FROM audit_spans") > 0
    r = api.post("/api/scenarios/self_service_requests/start", json={"speed": 60})
    assert r.status_code == 200 and r.json()["active"] and r.json()["total"] == 2
    wait_for(lambda: api.get("/api/scenarios/status").json()["active"] is False, 30)
    wait_for(lambda: api.get("/api/overview").json()["pending_events"] == 0, 20)
    run_id = api.get("/api/scenarios/status").json()["run"]["run_id"]
    live = api.get("/api/live-feed").json()
    assert live["raw_alerts"] == 0 and live["sr_count"] == 2
    by_type = {s["request_type"]: s for s in live["service_requests"]}
    reset, admin = by_type["Password Reset"], by_type["Access Provisioning"]
    assert reset["status"] == "Completed" and reset["outcome"] == "password reset (per KB4001)"
    assert any(t["text"].startswith("Guardrail: low risk") for t in reset["trace"])
    assert admin["status"] == "Pending Approval" and any(t["text"].startswith("Guardrail: high risk") for t in admin["trace"])
    q = api.get("/api/approvals", params={"status": "open"}).json()
    assert [a["entity_ref"] for a in q] == [admin["sr_id"]]
    # Starting the live scenario wiped the earlier trace; only this run is in the audit trail.
    assert db.scalar("SELECT COUNT(*) FROM audit_spans WHERE run_id IS NULL OR run_id!=?", (run_id,)) == 0
    traces = api.get("/api/audit/traces", params={"run_id": run_id}).json()
    assert sorted(t["entity_ref"] for t in traces) == sorted([reset["sr_id"], admin["sr_id"]])
    m = api.get("/api/audit/metrics", params={"run_id": run_id}).json()
    assert {"sr_fulfilment", "guardrail", "executor"} <= set(m["agents"])


def test_unknown_scenario_and_missing_incident(api):
    assert api.post("/api/scenarios/nope/start", json={}).status_code == 404
    assert api.get("/api/incidents/INC404").status_code == 404
    assert api.post("/api/approvals/9999/decide", json={"approve": True}).status_code == 404


def test_graph_diagram_endpoint(api):
    m = api.get("/api/graph").json()["mermaid"]
    for node in ("route_event", "alert_correlation", "guardrail", "human_queue", "p1_surge"):
        assert node in m


def test_admin_reset_rebuilds_the_database(api):
    api.post("/api/self-service", json={"employee_id": "EMP1002", "request_type": "Password Reset"})
    db.execute("INSERT INTO kpi_results (created, payload) VALUES ('t', '{}')")
    kpis_before = db.scalar("SELECT COUNT(*) FROM kpi_results")
    assert api.post("/api/admin/reset").json()["ok"]
    assert db.scalar("SELECT COUNT(*) FROM service_requests WHERE origin!='seed'") == 0
    assert db.scalar("SELECT COUNT(*) FROM kpi_results") == kpis_before       # separate replay, survives a reset
    assert db.scalar("SELECT COUNT(*) FROM kb_articles WHERE score IS NOT NULL") == 30


def test_reset_scores_the_kb_without_claude_and_restarts_the_clock(api, monkeypatch):
    from app.llm import client as llm
    from app.runtime.clock import clock
    from app import settings

    class NoCalls:
        def __getattr__(self, name):
            raise AssertionError("reset must not call Claude")
    monkeypatch.setenv("LLM_MODE", "live")
    llm.set_client(NoCalls())
    clock.advance(3 * 86400)
    assert api.post("/api/admin/reset").json()["ok"]
    assert db.scalar("SELECT COUNT(*) FROM kb_articles WHERE assessment_mode='rules'") == 30
    assert db.scalar("SELECT COUNT(*) FROM llm_calls") == 0
    assert abs((clock.now() - settings.sim_start()).total_seconds()) < 60


def test_audit_traces_metrics_and_export(api):
    api.post("/api/self-service", json={"employee_id": "EMP1002", "request_type": "Password Reset"})
    traces = wait_for(lambda: api.get("/api/audit/traces", params={"limit": 50}).json())
    t = next(t for t in traces if t["event_type"] == "sr.submitted")
    assert t["steps"] >= 3 and t["tool_calls"] >= 1 and t["latency_ms"] is not None
    assert "sr_fulfilment" in t["agents"]
    detail = api.get(f"/api/audit/traces/{t['trace_id']}").json()
    assert detail["event"]["type"] == "sr.submitted"
    assert {s["span_type"] for s in detail["spans"]} >= {"node", "tool", "guardrail"}
    assert detail["decisions"] and all(d["trace_id"] == t["trace_id"] for d in detail["decisions"])
    m = api.get("/api/audit/metrics").json()
    assert set(m["totals"]) == {"iteration", "tools", "tokens", "accuracy", "activity", "latency"}
    assert m["agents"]["sr_fulfilment"]["tools"]["calls"] >= 1
    assert m["totals"]["tokens"]["calls"] == 0                        # rules mode: no model calls
    csv_text = api.get("/api/audit/export", params={"table": "spans"}).text
    assert csv_text.splitlines()[0].startswith("span_id,trace_id")
    assert api.get("/api/audit/export", params={"table": "nope"}).status_code == 400
    assert api.get("/api/audit/traces/TR-missing").status_code == 404


def test_correlation_tracking_records_grouping_and_confidence(api):
    api.post("/api/scenarios/licensecore_outage/start", json={"speed": 800})
    wait_for(lambda: api.get("/api/scenarios/status").json()["active"] is False, 30)
    wait_for(lambda: api.get("/api/overview").json()["pending_events"] == 0, 20)
    body = api.get("/api/audit/correlation", params={"limit": 500}).json()
    rows = body["rows"]
    assert rows and {r["grouping"] for r in rows} <= {"new_cluster", "duplicate", "correlated"}
    assert all(0.0 <= r["confidence"] <= 1.0 and r["cluster_id"] and r["alert_id"] for r in rows)
    assert body["how_it_works"] and all(r["explanation"] for r in rows)
    assert {s["grouping"] for s in body["summary"]} == {r["grouping"] for r in rows}
    one = api.get("/api/audit/correlation", params={"cluster_id": rows[0]["cluster_id"]}).json()["rows"]
    assert one and all(r["cluster_id"] == rows[0]["cluster_id"] for r in one)
    assert api.get("/api/audit/export", params={"table": "correlation", "format": "jsonl"}).status_code == 200
    api.post("/api/admin/reset")
    assert db.scalar("SELECT COUNT(*) FROM correlation_log") == 0
