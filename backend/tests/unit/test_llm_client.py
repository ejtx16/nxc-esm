"""LLM wrapper behaviour with a fake SDK client (no network)."""
from types import SimpleNamespace

import anthropic
import httpx
import pytest

from app.data import db
from app.llm import client as llm
from app.llm.schemas import AlertAssessment, SRIntent


class FakeBeta:
    def __init__(self, behaviour):
        self.behaviour, self.calls = behaviour, []
        self.messages = self

    def parse(self, **kw):
        self.calls.append(kw)
        return self.behaviour(kw)


def fake(behaviour):
    c = SimpleNamespace(beta=FakeBeta(behaviour))
    llm.set_client(c)
    return c.beta


def ok_response(**extra):
    usage = SimpleNamespace(input_tokens=80, output_tokens=40, cache_read_input_tokens=700, cache_creation_input_tokens=0)
    return SimpleNamespace(stop_reason="end_turn", usage=usage, parsed_output=AlertAssessment(
        cluster_id="CL-1", classification="false_positive", confidence=0.9, rationale="r"), **extra)


def call(**kw):
    return llm.parse(AlertAssessment, agent="alert_correlation", system="S", context="C", prompt="P", **kw)


def test_rules_mode_never_calls_the_model(fresh):
    beta = fake(lambda kw: ok_response())
    assert call() is None and beta.calls == []


def test_live_mode_passes_effort_cache_fallbacks_and_schema(fresh, monkeypatch):
    db.set_setting("llm_mode", "live")
    beta = fake(lambda kw: ok_response())
    out = call(effort="medium")
    assert out.classification == "false_positive"
    kw = beta.calls[0]
    assert kw["model"] == "claude-opus-5-5"
    assert kw["output_config"] == {"effort": "medium"}
    assert kw["output_format"] is AlertAssessment
    assert kw["fallbacks"] == "default" and kw["betas"] == ["server-side-fallback-2026-07-01"]
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "tool_choice" not in kw and "temperature" not in kw
    assert kw["thinking"] == {"type": "adaptive", "display": "summarized"}     # audit: summarized reasoning
    row = db.one("SELECT * FROM llm_calls")
    assert row["agent"] == "alert_correlation" and row["cache_read"] == 700 and row["error"] is None
    assert row["model"] == "claude-opus-5-5" and row["attempt"] == 1
    assert row["cost_usd"] == round((80 * 4 + 40 * 20 + 700 * 0.2) / 1e6, 6)


def test_empty_context_sends_no_cached_empty_block(fresh):
    db.set_setting("llm_mode", "live")
    beta = fake(lambda kw: ok_response())
    llm.parse(AlertAssessment, agent="kb_curation", system="s", context="", prompt="p")
    assert beta.calls[0]["messages"][0]["content"] == [{"type": "text", "text": "p"}]


def test_refusal_returns_none_and_is_logged(fresh):
    db.set_setting("llm_mode", "live")
    fake(lambda kw: SimpleNamespace(stop_reason="refusal", usage=None, parsed_output=None))
    assert call() is None
    assert db.one("SELECT * FROM llm_calls")["error"] == "refusal"


def test_timeout_raises_llm_timeout_for_the_human_queue(fresh):
    db.set_setting("llm_mode", "live")
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

    def boom(kw):
        raise anthropic.APITimeoutError(request=req)
    fake(boom)
    with pytest.raises(llm.LLMTimeout):
        call()


def test_other_api_errors_fall_back_to_rules(fresh):
    db.set_setting("llm_mode", "live")
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

    def boom(kw):
        raise anthropic.RateLimitError("slow down", response=httpx.Response(429, request=req), body=None)
    fake(boom)
    assert call() is None
    rows = db.query("SELECT * FROM llm_calls ORDER BY id")
    assert [r["attempt"] for r in rows] == [1, 2, 3]                 # each retry attempt is audited
    assert all("RateLimitError" in r["error"] for r in rows)


def test_retry_then_success_is_audited_per_attempt_with_thinking_summary(fresh):
    db.set_setting("llm_mode", "live")
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    calls = {"n": 0}

    def flaky(kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise anthropic.RateLimitError("slow down", response=httpx.Response(429, request=req), body=None)
        return ok_response(content=[SimpleNamespace(type="thinking", thinking="Noise history is high."),
                                    SimpleNamespace(type="text", text="{}")])
    fake(flaky)
    assert call().classification == "false_positive"
    rows = db.query("SELECT * FROM llm_calls ORDER BY id")
    assert [(r["attempt"], r["error"] is None) for r in rows] == [(1, False), (2, True)]
    assert rows[1]["thinking_summary"] == "Noise history is high."
    spans = db.query("SELECT * FROM audit_spans WHERE span_type='llm' ORDER BY started_real")
    assert [(s["attempt"], s["status"]) for s in spans] == [(1, "error"), (2, "ok")]
    assert rows[1]["span_id"] == spans[1]["span_id"]


def test_timeout_is_not_retried(fresh):
    db.set_setting("llm_mode", "live")
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

    def boom(kw):
        raise anthropic.APITimeoutError(request=req)
    beta = fake(boom)
    with pytest.raises(llm.LLMTimeout):
        call()
    assert len(beta.calls) == 1
    assert db.one("SELECT status FROM audit_spans WHERE span_type='llm'")["status"] == "timeout"


def test_missing_parsed_output_falls_back(fresh):
    db.set_setting("llm_mode", "live")
    fake(lambda kw: SimpleNamespace(stop_reason="end_turn", usage=None, parsed_output=None))
    assert call() is None


def test_llm_timeout_in_triage_routes_to_human_queue(fresh):
    """OR-4: an LLM timeout becomes a manual-review approval with reason agent_failure; the flow is not blocked."""
    from tests.conftest import send_alert
    db.set_setting("llm_mode", "live")
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

    def boom(kw):
        raise anthropic.APITimeoutError(request=req)
    fake(boom)
    send_alert()                       # Critical fast path ignores the timeout, so ack + incident still happen
    inc = db.one("SELECT * FROM incidents WHERE origin!='seed'")
    assert inc and db.one("SELECT * FROM clusters")["ack_ts"]
    ap = db.one("SELECT * FROM approvals WHERE action='manual_review'")
    assert ap and ap["reason_code"] == "agent_failure" and ap["agent"] == "incident_triage"
    assert db.scalar("SELECT COUNT(*) FROM agent_decisions WHERE mode='error'") == 1


def test_live_triage_decision_is_validated_against_provided_context(fresh):
    from app.agents import incident_triage as tri
    from app.llm.schemas import TriageDecision
    db.set_setting("llm_mode", "live")
    fake(lambda kw: SimpleNamespace(stop_reason="end_turn", usage=None, parsed_output=TriageDecision(
        category="Access Issue", priority="P2", team="Made Up Team", confidence=0.77, rationale="because",
        similar_incident_ids=["INC9999"], kb_article_ids=["KB9999"], auto_resolvable=True)))
    d = tri.decide("SSO login failing after certificate renewal", allow_llm=True)
    assert d["mode"] == "llm" and d["category"] == "Access Issue"
    assert d["team"] in {"App Support L1", "App Support L2", "Database Team", "Infra Team", "Security Ops"}   # invented team rejected
    assert d["auto_resolvable"] is True and d["runbook"] is None or d["runbook"]                              # cannot invent runbooks
    assert not d["auto_resolvable"] or d["runbook"]
    assert all(a["article_id"] != "KB9999" for a in d["kb_good"])


def test_live_sr_intent_checks_free_text_and_forms(fresh):
    from tests.conftest import make_sr
    db.set_setting("llm_mode", "live")
    beta = fake(lambda kw: SimpleNamespace(stop_reason="end_turn", usage=None, parsed_output=SRIntent(
        request_type="Password Reset", target_system=None, access_level=None, eligible_for_self_service=True, rationale="r")))
    sid = make_sr("EMP1002", None, "portal", text="uh my login is borked")
    assert len(beta.calls) == 1
    assert db.one("SELECT * FROM service_requests WHERE sr_id=?", (sid,))["status"] == "Completed"
    sid = make_sr("EMP1002", "Password Reset", "portal")           # structured form: Claude checks it too
    assert len(beta.calls) == 2
    assert db.one("SELECT * FROM service_requests WHERE sr_id=?", (sid,))["request_type"] == "Password Reset"
