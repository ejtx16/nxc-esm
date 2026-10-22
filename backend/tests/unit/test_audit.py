"""Audit trail (ADR-0004, DR-0009): traces, spans and the six audit dimensions."""
import sqlite3

import pytest

from app.data import audit_ctx, db
from app.runtime import approvals
from tests.conftest import make_sr, send_alert


def _spans(trace_id):
    return db.query("SELECT * FROM audit_spans WHERE trace_id=? ORDER BY started_real", (trace_id,))


def test_every_node_is_a_span_with_increasing_iteration(fresh):
    send_alert()
    tid = db.one("SELECT trace_id FROM agent_decisions WHERE kind='agent' AND agent='alert_correlation'")["trace_id"]
    nodes = [s for s in _spans(tid) if s["span_type"] == "node"]
    assert [n["name"] for n in nodes][:3] == ["route_event", "alert_correlation", "guardrail"]
    assert [n["iteration"] for n in nodes] == list(range(1, len(nodes) + 1))
    assert all(n["latency_ms"] is not None and n["status"] == "ok" for n in nodes)


def test_decisions_spans_and_tools_share_the_trace(fresh):
    send_alert()
    dec = db.one("SELECT * FROM agent_decisions WHERE kind='agent' AND agent='alert_correlation'")
    node = db.one("SELECT * FROM audit_spans WHERE span_id=?", (dec["span_id"],))
    assert node["name"] == "alert_correlation" and node["decision_id"] == dec["decision_id"]
    assert dec["iteration"] == node["iteration"]
    spans = _spans(dec["trace_id"])
    guard = [s for s in spans if s["span_type"] == "guardrail"]
    assert guard and all(s["decision_id"] for s in guard)
    tools = {s["name"] for s in spans if s["span_type"] == "tool"}
    assert "mock_servicenow.app_tier" in tools                         # read-only tool call
    assert "executor.acknowledge" in tools and "executor.open_incident" in tools   # side-effecting actions
    ex = next(s for s in spans if s["name"] == "executor.acknowledge")
    assert db.loads(ex["input_json"])["side_effect"] is True


def test_tool_reads_can_be_switched_off(fresh, monkeypatch):
    monkeypatch.setitem(audit_ctx.DEFAULTS, "log_tool_reads", False)
    monkeypatch.setattr(audit_ctx, "cfg", lambda: {**audit_ctx.DEFAULTS, "log_tool_reads": False})
    send_alert()
    names = {r["name"] for r in db.query("SELECT name FROM audit_spans WHERE span_type='tool'")}
    assert names and all(n.startswith(("executor.", "mock_servicenow.notify_")) for n in names)  # side effects only


def test_queued_approvals_and_human_decisions_are_audited_with_accuracy(fresh):
    db.set_setting("kill_switch", {"global": True, "agents": {}, "apps": {}})
    send_alert()
    queued = db.query("SELECT * FROM agent_decisions WHERE kind='queued'")
    assert queued and all(q["trace_id"] for q in queued)
    ap = db.one("SELECT * FROM approvals WHERE action='acknowledge'")
    approvals.decide(ap["approval_id"], True, "tester")
    human = db.one("SELECT * FROM audit_spans WHERE span_type='human'")
    assert human["name"] == "approve" and human["trace_id"].startswith("TR-op-")
    acc = db.one("SELECT * FROM accuracy_results WHERE source='human_override'")
    assert acc["agent"] == "alert_correlation" and acc["correct"] == 1
    ap2 = db.one("SELECT * FROM approvals WHERE action='open_incident'")
    approvals.decide(ap2["approval_id"], False, "tester")
    assert db.scalar("SELECT correct FROM accuracy_results WHERE source='human_override' ORDER BY id DESC") == 0


def test_alert_classification_accuracy_recorded_against_ground_truth(fresh):
    send_alert(severity="Low", name="Disk usage > 85%", system="IntegrationHub", fp=True, recovered=True)
    acc = db.one("SELECT * FROM accuracy_results WHERE source='true_positive'")
    assert acc["metric"] == "alert_classification" and acc["expected"] == "false_positive"
    assert acc["decision_id"] and acc["trace_id"]


def test_sr_flow_traces_tools_and_executor(fresh):
    sid = make_sr()
    tid = db.one("SELECT trace_id FROM agent_decisions WHERE entity_ref=? AND kind='agent'", (sid,))["trace_id"]
    names = [s["name"] for s in _spans(tid)]
    assert "mock_iam.lookup" in names and "executor.reset_password" in names


def test_new_tables_are_append_only(fresh):
    send_alert()
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("UPDATE audit_spans SET status='x'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("DELETE FROM accuracy_results")


def test_redaction_masks_emails_and_truncates(monkeypatch):
    monkeypatch.setattr(audit_ctx, "cfg", lambda: {**audit_ctx.DEFAULTS, "max_payload_chars": 40})
    out = audit_ctx.redact({"reported_by": "user_854@nexacore.com", "text": "x" * 100})
    assert "user_854" not in out and "***@***" in out and out.endswith("chars]")


def test_cost_uses_configured_pricing():
    assert audit_ctx.cost_usd(1_000_000, 0, 0, 0) == 4.0
    assert audit_ctx.cost_usd(None, None, None, None) is None


def test_migrate_adds_columns_to_an_old_database(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(  # the pre-audit-trail schema
        "CREATE TABLE agent_decisions (decision_id INTEGER PRIMARY KEY AUTOINCREMENT, parent_id INTEGER, kind TEXT, "
        "run_id TEXT, event_id INTEGER, agent TEXT, entity_ref TEXT, input_summary TEXT, reasoning TEXT, "
        "confidence REAL, proposed_action TEXT, guardrail_verdict TEXT, guardrail_reason TEXT, human_decision TEXT, "
        "mode TEXT, latency_ms REAL, timestamp TEXT, ts_real REAL)")
    conn.execute("CREATE TABLE llm_calls (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, agent TEXT, schema TEXT, "
                 "effort TEXT, input_tokens INTEGER, output_tokens INTEGER, cache_read INTEGER, cache_write INTEGER, "
                 "latency_ms REAL, stop_reason TEXT, error TEXT)")
    conn.commit()
    conn.close()
    db.configure(path)
    cols = {r["name"] for r in db.query("PRAGMA table_info(llm_calls)")}
    assert {"trace_id", "cost_usd", "attempt", "thinking_summary"} <= cols
    assert "iteration" in {r["name"] for r in db.query("PRAGMA table_info(agent_decisions)")}
