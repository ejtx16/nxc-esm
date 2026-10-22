"""BDD step definitions for the six demo items (D1-D6). One .feature per item."""
from __future__ import annotations

import csv
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from app import settings
from app.agents import guardrail as gr, incident_triage as tri
from app.data import db
from app.graph.state import ActionProposal
from app.runtime import approvals, bus, engine, player
from app.runtime.clock import clock, iso
from tests.conftest import make_alert, make_sr, send_alert

FEATURES = Path(__file__).parent.parent / "features"
for f in sorted(FEATURES.glob("d*.feature")):
    scenarios(str(f))


@pytest.fixture
def ctx():
    return {}


# --- shared givens --------------------------------------------------------------------------------
@given("the seeded demo database")
def _seeded(fresh):
    pass


@given("the LicenseCore outage scenario has created a P1 incident")
def _outage(fresh, ctx):
    player.run_sync("licensecore_outage")
    ctx["incident_id"] = db.one("SELECT incident_id FROM incidents WHERE priority='P1' AND origin!='seed'")["incident_id"]


@given("the global kill switch is on")
def _kill(fresh):
    db.set_setting("kill_switch", {"global": True, "agents": {}, "apps": {}})


# --- D1 -------------------------------------------------------------------------------------------
def _burst(n, name, system, severity, minutes, start=datetime(2025, 8, 4, 9, 0)):
    step = minutes * 60 / n
    for i in range(n):
        send_alert(system=system, name=name, severity=severity, ts=iso(start + timedelta(seconds=i * step)))


@when(parsers.parse('{n:d} duplicate "{name}" {severity} alerts arrive from "{system}" over {minutes:d} minutes'))
def _dups(fresh, n, name, severity, system, minutes):
    _burst(n, name, system, severity, minutes)


@when(parsers.parse('a self-recovering {severity} alert "{name}" arrives from "{system}"'))
def _recovering(fresh, severity, name, system):
    send_alert(system=system, name=name, severity=severity, fp=True, recovered=True)


@then(parsers.parse("exactly {n:d} alert cluster exists"))
def _n_clusters(n):
    assert db.scalar("SELECT COUNT(*) FROM clusters") == n


@then(parsers.parse("that cluster contains {n:d} alerts"))
def _cluster_size(n):
    assert db.one("SELECT alert_count FROM clusters")["alert_count"] == n


@then(parsers.parse("exactly {n:d} incident is open for that cluster"))
def _n_incidents(n):
    assert db.scalar("SELECT COUNT(*) FROM incidents WHERE origin!='seed'") == n
    assert db.scalar("SELECT COUNT(*) FROM incidents WHERE source_cluster_id=(SELECT cluster_id FROM clusters)") == n


@then(parsers.parse("the cluster was acknowledged within {s:d} seconds of its first alert"))
def _acked(s):
    assert db.one("SELECT ack_latency_s FROM clusters")["ack_latency_s"] <= s


@then(parsers.parse("the cluster is suppressed as a false positive with confidence of at least {c:f}"))
def _suppressed(c):
    cl = db.one("SELECT * FROM clusters")
    assert cl["status"] == "suppressed" and cl["classification"] == "false_positive" and cl["confidence"] >= c


@then("no incident is opened")
def _no_incident():
    assert db.scalar("SELECT COUNT(*) FROM incidents WHERE origin!='seed'") == 0


@then("the audit log records the suppression verdict")
def _audit_suppress():
    row = db.one("SELECT * FROM agent_decisions WHERE kind='guardrail' AND entity_ref=(SELECT cluster_id FROM clusters)")
    assert row["guardrail_verdict"] == "Approve" and row["reasoning"]
    assert db.one("SELECT * FROM agent_decisions WHERE kind='execution' AND reasoning LIKE '%suppressed%'")


@then("the cluster is not suppressed")
def _not_suppressed():
    assert db.one("SELECT status FROM clusters")["status"] != "suppressed"


# --- D2 -------------------------------------------------------------------------------------------
@then(parsers.parse('the incident is classified as {prio} and routed to "{team}"'))
def _classified(ctx, prio, team):
    i = db.one("SELECT * FROM incidents WHERE incident_id=?", (ctx["incident_id"],))
    assert i["priority"] == prio and i["assigned_team"] == team and i["category"]


@then(parsers.parse('the triage rationale mentions "{text}"'))
def _rationale(ctx, text):
    assert text in db.one("SELECT rationale FROM incidents WHERE incident_id=?", (ctx["incident_id"],))["rationale"]


@then("the incident was escalated immediately")
def _escalated(ctx):
    assert db.one("SELECT escalated FROM incidents WHERE incident_id=?", (ctx["incident_id"],))["escalated"] == 1


@then(parsers.parse("no KB article below {pct:d} percent accuracy is attached"))
def _no_low_kb(ctx, pct):
    ids = db.loads(db.one("SELECT kb_article_ids FROM incidents WHERE incident_id=?", (ctx["incident_id"],))["kb_article_ids"], [])
    for aid in ids:
        assert db.one("SELECT score FROM kb_articles WHERE article_id=?", (aid,))["score"] >= pct


@then("the rationale explains which articles were excluded")
def _excluded(ctx):
    assert "excluded" in db.one("SELECT rationale FROM incidents WHERE incident_id=?", (ctx["incident_id"],))["rationale"]


@given(parsers.parse("an assigned P2 incident with an {h:d} hour SLA"))
def _p2(fresh, h):
    db.insert("incidents", {"incident_id": "INC2900", "priority": "P2", "status": "Assigned", "origin": "scenario",
                            "short_description": "Payment gateway down", "sla_hours": h, "opened_ts": clock.now_iso(),
                            "assigned_team": "App Support L2", "app_id": "BillingEngine", "escalated": 0})


@when(parsers.parse("{h:f} hours pass"))
def _hours(h):
    clock.advance(h * 3600)
    engine.sla_scan()
    engine.process_pending()


@then("the incident is escalated before the SLA is breached")
def _esc_before():
    i = db.one("SELECT * FROM incidents WHERE incident_id='INC2900'")
    assert i["escalated"] == 1 and i["breach_risk"] < 1.0


@given("the golden triage labels")
def _golden(fresh, ctx):
    ctx["golden"] = list(csv.DictReader(open(settings.GOLDEN_PATH)))


@when("the triage agent classifies each golden incident")
def _classify_golden(ctx):
    ctx["results"] = [(tri.decide(r["description"], allow_llm=False), r) for r in ctx["golden"]]


@then(parsers.parse("category accuracy is at least {pct:d} percent"))
def _cat_acc(ctx, pct):
    ok = sum(d["category"] == r["category"] for d, r in ctx["results"])
    assert 100 * ok / len(ctx["results"]) >= pct


@then(parsers.parse("priority accuracy within one level is at least {pct:d} percent"))
def _prio_acc(ctx, pct):
    ok = sum(abs(int(d["priority"][1]) - int(r["priority"][1])) <= 1 for d, r in ctx["results"])
    assert 100 * ok / len(ctx["results"]) >= pct


# --- D3 -------------------------------------------------------------------------------------------
@given(parsers.parse("the triage agent proposes auto-resolution with confidence {c:f}"))
def _proposal(ctx):
    ap = db.one("SELECT * FROM approvals WHERE action='resolve' AND entity_ref=?", (ctx["incident_id"],))
    ctx["proposal"] = ActionProposal(**db.loads(ap["proposal"]))
    assert ctx["proposal"].confidence == pytest.approx(0.92)


@when("the guardrail evaluates the proposal")
def _evaluate(ctx):
    ctx["verdict"] = gr.evaluate(ctx["proposal"])


@then(parsers.parse('the verdict is "{verdict}"'))
def _verdict(ctx, verdict):
    assert ctx["verdict"].verdict == verdict


@then(parsers.parse('the failed rules include "{a}" and "{b}"'))
def _failed_rules(ctx, a, b):
    assert {a, b} <= set(ctx["verdict"].failed_rules)


@then("an approval request appears in the guardrail queue")
def _in_queue(ctx):
    ap = db.one("SELECT * FROM approvals WHERE action='resolve' AND entity_ref=?", (ctx["incident_id"],))
    assert ap["status"] == "pending"


@then("an audit record is written with the verdict and reasons")
def _audit(ctx):
    rows = db.query("SELECT * FROM agent_decisions WHERE kind='guardrail' AND entity_ref=? AND guardrail_verdict='RequireApproval'",
                    (ctx["incident_id"],))
    resolve = [r for r in rows if "Tier 0" in (r["reasoning"] or "") or "allowed" in (r["reasoning"] or "").lower() or "P1" in (r["reasoning"] or "")]
    assert resolve and "failed" in resolve[0]["guardrail_reason"]


@when("a human approves the blocked auto-resolution")
def _human_approves(ctx):
    ap = db.one("SELECT approval_id FROM approvals WHERE action='resolve' AND entity_ref=?", (ctx["incident_id"],))
    assert approvals.decide(ap["approval_id"], True, "svc.delivery.mgr")["ok"]
    engine.process_pending()


@then("the incident is resolved by its runbook")
def _resolved(ctx):
    i = db.one("SELECT * FROM incidents WHERE incident_id=?", (ctx["incident_id"],))
    assert i["status"] == "Resolved" and "RB-LC-01" in i["resolution_notes"]


@then("surge mode is switched off")
def _surge_off():
    assert db.get_setting("surge_mode") is False


@given("a P3 incident on a Tier-3 application with a trusted runbook and KB article outside any freeze window")
def _low_risk(fresh, ctx):
    clock.reset(datetime(2025, 8, 12, 10, 0))
    ctx["proposal"] = ActionProposal(agent="incident_triage", action="resolve", entity_ref="INC1", summary="x", confidence=0.92,
                                     context={"priority": "P3", "app_tier": 3, "app_id": "NotificationService",
                                              "reversible": True, "kb_accuracy": 96.0, "blast_radius_users": 10})


@when(parsers.parse("the guardrail evaluates a resolve proposal with confidence {c:f}"))
def _evaluate_resolve(ctx, c):
    ctx["proposal"].confidence = c
    ctx["verdict"] = gr.evaluate(ctx["proposal"])


@when(parsers.parse('a Critical "{system}" alert arrives'))
def _crit(system):
    send_alert(system=system)


@then("no incident is opened autonomously")
def _none_open():
    assert db.scalar("SELECT COUNT(*) FROM incidents WHERE origin!='seed'") == 0


@then(parsers.parse('approvals are waiting for "{a}" and "{b}"'))
def _waiting(a, b):
    pending = {r["action"] for r in db.query("SELECT action FROM approvals WHERE status='pending'")}
    assert {a, b} <= pending


# --- D4 -------------------------------------------------------------------------------------------
@when(parsers.parse('employee "{emp}" submits a self-service "{rtype}"'))
def _submit(fresh, ctx, emp, rtype):
    ctx["sr_id"] = make_sr(emp, rtype, "portal")


@when(parsers.parse('employee "{emp}" submits a self-service "{rtype}" for "{system}" at "{level}" level'))
def _submit_access(fresh, ctx, emp, rtype, system, level):
    ctx["sr_id"] = make_sr(emp, rtype, "portal", system=system, level=level)


def _sr(ctx):
    return db.one("SELECT * FROM service_requests WHERE sr_id=?", (ctx["sr_id"],))


@then("the request is completed by the agent")
def _sr_done(ctx):
    r = _sr(ctx)
    assert r["status"] == "Completed" and r["fulfilled_by"] == "agent:sr_fulfilment" and r["fulfillment_time_hrs"] < 1 / 60


@then("a temporary credential was issued through the mock IAM")
def _cred():
    assert db.scalar("SELECT COUNT(*) FROM iam_log WHERE action='reset_password'") == 1


@then("the fulfilment trace shows identity verification")
def _trace(ctx):
    assert "Identity verified" in " ".join(t["text"] for t in db.loads(_sr(ctx)["trace"]))


@then("the request is waiting for approval")
def _waiting_approval(ctx):
    assert _sr(ctx)["status"] == "Pending Approval"


@when(parsers.parse('the "{name}" scenario is replayed'))
def _replay(fresh, ctx, name):
    ctx["run_id"] = player.run_sync(name)


def _run_sr(ctx, rtype: str) -> dict:
    r = db.one("SELECT * FROM service_requests WHERE run_id=? AND request_type=?", (ctx["run_id"], rtype))
    assert r, f"no {rtype} in run {ctx['run_id']}"
    return r


@then("the password reset was approved as low risk and completed from the knowledge document")
def _low_risk_done(ctx):
    r = _run_sr(ctx, "Password Reset")
    trace = " | ".join(t["text"] for t in db.loads(r["trace"]))
    assert r["status"] == "Completed" and r["fulfilled_by"] == "agent:sr_fulfilment"
    assert "Guardrail: low risk" in trace and "Knowledge: KB" in trace and "Followed the solution in KB" in trace
    assert "(per KB" in r["outcome"]


@then("the admin access request was held as high risk for a human approver")
def _high_risk_held(ctx):
    r = _run_sr(ctx, "Access Provisioning")
    trace = " | ".join(t["text"] for t in db.loads(r["trace"]))
    assert r["status"] == "Pending Approval" and "Guardrail: high risk" in trace
    ap = db.one("SELECT * FROM approvals WHERE entity_ref=? AND action='grant_access'", (r["sr_id"],))
    assert ap["status"] == "pending" and "block_privileged_access" in db.loads(ap["failed_rules"])
    assert db.scalar("SELECT COUNT(*) FROM iam_log WHERE action='grant_access'") == 0


@then("both requests have a trace in the audit trail for that run")
def _both_traced(ctx):
    for rtype in ("Password Reset", "Access Provisioning"):
        sid = _run_sr(ctx, rtype)["sr_id"]
        kinds = {r["kind"] for r in db.query("SELECT kind FROM agent_decisions WHERE entity_ref=? AND run_id=?", (sid, ctx["run_id"]))}
        assert {"agent", "guardrail"} <= kinds
        assert db.scalar("SELECT COUNT(DISTINCT trace_id) FROM audit_spans WHERE entity_ref=? AND run_id=?", (sid, ctx["run_id"])) == 1


@then("no access was granted")
def _no_access():
    assert db.scalar("SELECT COUNT(*) FROM iam_log WHERE action='grant_access'") == 0


@given(parsers.parse("{n:d} password reset tickets are waiting in the human queue"))
def _queue(fresh, ctx, n):
    ctx["queued"] = [make_sr(f"EMP700{i}", "Password Reset", "ticket") for i in range(n)]
    assert all(db.one("SELECT status FROM service_requests WHERE sr_id=?", (s,))["status"] == "Pending Human" for s in ctx["queued"])


@when("a P1 incident is declared")
def _declare_p1():
    send_alert()
    engine.process_pending()


@then(parsers.parse("all {n:d} queued requests are fulfilled by the agent"))
def _drained(ctx, n):
    rows = [db.one("SELECT * FROM service_requests WHERE sr_id=?", (s,)) for s in ctx["queued"]]
    assert len(rows) == n and all(r["status"] == "Completed" and r["fulfilled_by"] == "agent:sr_fulfilment" for r in rows)


# --- D5 -------------------------------------------------------------------------------------------
@then(parsers.parse("all {n:d} KB articles have an accuracy score and a keep, update or archive recommendation"))
def _all_scored(n):
    rows = db.query("SELECT * FROM kb_articles")
    assert len(rows) == n and all(r["score"] is not None and r["recommendation"] in ("keep", "update", "archive") for r in rows)


@then(parsers.parse('article "{aid}" scores below the {pct:d} percent threshold'))
def _below(aid, pct):
    assert db.one("SELECT score FROM kb_articles WHERE article_id=?", (aid,))["score"] < pct


@then(parsers.parse('a draft update for "{aid}" is waiting for approval'))
def _draft_waiting(aid):
    d = db.one("SELECT * FROM kb_drafts WHERE article_id=?", (aid,))
    assert d["status"] == "pending"
    assert db.one("SELECT status FROM approvals WHERE action='update_kb_article' AND entity_ref=?", (aid,))["status"] == "pending"


@then("the draft removes the deprecated references")
def _draft_clean():
    from app.agents.kb_curation import deprecated_refs
    assert not deprecated_refs(db.one("SELECT new_body FROM kb_drafts")["new_body"])


@when(parsers.parse('the knowledge manager approves the draft for "{aid}"'))
def _km_approves(aid):
    ap = db.one("SELECT approval_id FROM approvals WHERE action='update_kb_article' AND entity_ref=?", (aid,))
    assert approvals.decide(ap["approval_id"], True, "knowledge.manager")["ok"]


@then(parsers.parse('article "{aid}" scores at least {pct:d} percent'))
def _at_least(aid, pct):
    assert db.one("SELECT score FROM kb_articles WHERE article_id=?", (aid,))["score"] >= pct


@then(parsers.parse('triage may now recommend article "{aid}"'))
def _recommendable(aid):
    from app.tools import kb_store
    assert kb_store.usable(kb_store.get(aid))


# --- D6 -------------------------------------------------------------------------------------------
@given("the KPI simulation has run on the provided datasets")
def _kpi(kpi_results, ctx):
    ctx["kpi"] = {k["key"]: k for k in kpi_results["kpis"]}
    ctx["results"] = kpi_results


@then(parsers.parse("{n:d} KPIs are reported with their baselines"))
def _n_kpis(ctx, n):
    assert len(ctx["kpi"]) == n and all(k["baseline"] is not None for k in ctx["kpi"].values())


@then(parsers.parse('each KPI is labelled "{a}" or "{b}"'))
def _labelled(ctx, a, b):
    assert {k["kind"] for k in ctx["kpi"].values()} <= {a, b}
    assert {k["key"] for k in ctx["kpi"].values() if k["kind"] == "measured"} >= {"alert_noise", "ack_time", "triage_time", "self_service", "sr_breach"}
    assert all(k["method"] for k in ctx["kpi"].values())


@then(parsers.parse("the alert noise ratio is below its baseline of {b:d} percent"))
def _noise(ctx, b):
    assert ctx["kpi"]["alert_noise"]["after"] < b


@then(parsers.parse("the critical alert ack time is under {m:d} minutes"))
def _ack(ctx, m):
    assert ctx["kpi"]["ack_time"]["after"] < m


@then(parsers.parse("the average triage time is under {m:d} minutes"))
def _triage(ctx, m):
    assert ctx["kpi"]["triage_time"]["after"] < m


@then(parsers.parse("the self-service rate is above {p:d} percent"))
def _ss(ctx, p):
    assert ctx["kpi"]["self_service"]["after"] > p


@then(parsers.parse("raw alert noise in the scenarios is within {pts:d} points of {base:d} percent"))
def _raw(ctx, pts, base):
    assert abs(ctx["kpi"]["alert_noise"]["detail"]["raw_noise_pct"] - base) <= pts


# --- D7 -------------------------------------------------------------------------------------------
@then("every processed event has an audit trace")
def _every_event_traced(fresh):
    done = db.scalar("SELECT COUNT(*) FROM events WHERE status='done'")
    traced = db.scalar("SELECT COUNT(DISTINCT event_id) FROM audit_spans WHERE event_id IS NOT NULL")
    assert done and traced == done


@then("the P1 triage trace records its steps, tool calls and latency")
def _p1_trace(fresh, ctx):
    tid = db.scalar("SELECT trace_id FROM agent_decisions WHERE kind='agent' AND agent='incident_triage' "
                    "AND entity_ref=? ORDER BY decision_id", (ctx["incident_id"],))
    spans = db.query("SELECT * FROM audit_spans WHERE trace_id=?", (tid,))
    nodes = sorted(s["iteration"] for s in spans if s["span_type"] == "node")
    assert nodes == list(range(1, len(nodes) + 1)) and len(nodes) >= 4
    assert any(s["name"] == "incident_triage.similar_incidents" for s in spans)
    assert any(s["span_type"] == "guardrail" and "resolve" in s["name"] for s in spans)
    assert all(s["latency_ms"] is not None for s in spans)


@then("alert classification accuracy is recorded against ground truth")
def _alert_accuracy(fresh):
    n = db.scalar("SELECT COUNT(*) FROM accuracy_results WHERE source='true_positive'")
    assert n and n == db.scalar("SELECT COUNT(*) FROM clusters WHERE run_id IS NOT NULL")


@then("the audit metrics cover iteration, tools, tokens, accuracy, activity and latency")
def _metrics(fresh):
    from app.api.routes import audit as audit_api
    m = audit_api.metrics()
    assert set(m["totals"]) == {"iteration", "tools", "tokens", "accuracy", "activity", "latency"}
    assert m["totals"]["accuracy"]["graded"] > 0 and m["totals"]["tools"]["calls"] > 0
    assert m["agents"]["guardrail"]["activity"]["guardrail_verdicts"]
