from app.data import db
from app.runtime import approvals, bus, engine
from tests.conftest import make_sr, send_alert


def sr(sid):
    return db.one("SELECT * FROM service_requests WHERE sr_id=?", (sid,))


def test_password_reset_completes_end_to_end_via_portal(fresh):
    sid = make_sr("EMP1002", "Password Reset", "portal")
    r = sr(sid)
    assert r["status"] == "Completed" and r["fulfilled_by"] == "agent:sr_fulfilment"
    assert r["fulfillment_time_hrs"] < 0.01 and r["sla_met"] == "Yes"
    assert db.scalar("SELECT COUNT(*) FROM iam_log WHERE action='reset_password' AND employee_id='EMP1002'") == 1
    trace = " ".join(t["text"] for t in db.loads(r["trace"]))
    assert "Identity verified" in trace and "Password reset issued" in trace


def test_fulfilment_cites_the_knowledge_document_and_the_guardrail_risk(fresh):
    sid = make_sr("EMP1002", "Password Reset", "portal")
    r = sr(sid)
    lines = [t["text"] for t in db.loads(r["trace"])]
    assert any(l.startswith("Knowledge: KB4001 'How to reset user password via admin console'") for l in lines)
    assert any(l.startswith("Guardrail: low risk - approved to run without a person") for l in lines)
    assert any(l.startswith("Followed the solution in KB4001:") for l in lines)
    assert r["outcome"] == "password reset (per KB4001)"
    assert db.one("SELECT 1 FROM audit_spans WHERE entity_ref=? AND name='kb_store.solution_for'", (sid,))
    g = db.one("SELECT reasoning, guardrail_reason FROM agent_decisions WHERE kind='guardrail' AND entity_ref=?", (sid,))
    assert g["reasoning"].startswith("low risk:") and db.loads(g["guardrail_reason"])["risk"] == "low"


def test_held_request_is_marked_high_risk_on_its_trace(fresh):
    sid = make_sr("EMP1003", "Access Provisioning", "portal", system="BillingEngine", level="admin")
    lines = [t["text"] for t in db.loads(sr(sid)["trace"])]
    assert any(l.startswith("Guardrail: high risk - needs a human approver") for l in lines)
    assert any(l.startswith("Knowledge: KB4015") for l in lines)


def test_free_text_request_is_parsed_by_rules(fresh):
    sid = make_sr("EMP1004", None, "portal", text="I'm locked out, please reset my password")
    assert sr(sid)["request_type"] == "Password Reset" and sr(sid)["status"] == "Completed"
    sid = make_sr("EMP1004", None, "portal", text="Please give me access to WorkflowEngine")
    r = sr(sid)
    assert r["request_type"] == "Access Provisioning" and r["target_system"] == "WorkflowEngine" and r["status"] == "Completed"


def test_unknown_or_terminated_employee_is_rejected_without_action(fresh):
    for emp in ("EMP0000", "EMP9999"):
        sid = make_sr(emp, "Password Reset", "portal")
        assert sr(sid)["status"] == "Rejected"
    assert db.scalar("SELECT COUNT(*) FROM iam_log") == 0


def test_standard_access_within_entitlement_is_granted(fresh):
    sid = make_sr("EMP1002", "Access Provisioning", "portal", system="LicenseCore", level="standard")   # Agent role
    assert sr(sid)["status"] == "Completed"
    assert db.one("SELECT * FROM iam_log WHERE action='grant_access'")["system"] == "LicenseCore"


def test_access_outside_role_entitlement_goes_to_a_human(fresh):
    sid = make_sr("EMP1002", "Access Provisioning", "portal", system="DataWarehouse", level="standard")
    r = sr(sid)
    assert r["status"] == "Pending Human"
    assert db.scalar("SELECT COUNT(*) FROM iam_log") == 0


def test_privileged_access_requires_human_approval_then_executes(fresh):
    sid = make_sr("EMP1003", "Access Provisioning", "portal", system="BillingEngine", level="admin")
    assert sr(sid)["status"] == "Pending Approval"
    ap = db.one("SELECT * FROM approvals WHERE entity_ref=?", (sid,))
    assert ap["verdict"] == "RequireApproval" and "block_privileged_access" in ap["failed_rules"]
    assert db.scalar("SELECT COUNT(*) FROM iam_log") == 0
    assert approvals.decide(ap["approval_id"], True, "iam.approver")["ok"]
    r = sr(sid)
    assert r["status"] == "Completed" and r["fulfilled_by"] == "approved:iam.approver"


def test_rejecting_privileged_request_closes_it(fresh):
    sid = make_sr("EMP1003", "Access Provisioning", "portal", system="BillingEngine", level="admin")
    ap = db.one("SELECT approval_id FROM approvals WHERE entity_ref=?", (sid,))
    approvals.decide(ap["approval_id"], False, "iam.approver")
    assert sr(sid)["status"] == "Rejected"


def test_non_standard_request_type_routes_to_human(fresh):
    sid = make_sr("EMP1002", "Software Installation", "portal")
    assert sr(sid)["status"] == "Pending Human"


def test_ticket_channel_is_assisted_not_automatic_when_surge_is_off(fresh):
    sid = make_sr("EMP7001", "Password Reset", "ticket")
    assert sr(sid)["status"] == "Pending Human"


def test_surge_mode_absorbs_ticket_queue_automatically(fresh):
    ids = [make_sr(f"EMP700{i}", "Password Reset", "ticket") for i in range(5)]
    assert all(sr(i)["status"] == "Pending Human" for i in ids)
    send_alert()                                                       # P1 -> surge mode on -> drain
    engine.process_pending()
    assert db.get_setting("surge_mode") is True
    assert all(sr(i)["status"] == "Completed" for i in ids)
    assert all(sr(i)["fulfilled_by"] == "agent:sr_fulfilment" for i in ids)
    new = make_sr("EMP7006", "Password Reset", "ticket")
    assert sr(new)["status"] == "Completed"                              # new ticket requests also absorbed


def test_kill_switch_on_sr_agent_sends_resets_to_approval(fresh):
    db.set_setting("kill_switch", {"global": False, "agents": {"sr_fulfilment": True}, "apps": {}})
    sid = make_sr("EMP1002", "Password Reset", "portal")
    ap = db.one("SELECT * FROM approvals WHERE entity_ref=?", (sid,))
    assert ap["failed_rules"] == '["kill_switch"]' and sr(sid)["status"] == "Pending Approval"
