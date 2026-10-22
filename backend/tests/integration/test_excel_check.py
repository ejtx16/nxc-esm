"""Excel accuracy check: replays the curated Excel rows and grades every agent decision (rules mode)."""
from app.kpi import excel_check


def test_excel_check_grades_all_five_leading_indicators(fresh):
    res = excel_check.run_check()
    kpis = {k["key"]: k for k in res["kpis"]}
    assert list(kpis) == ["alert_noise", "ack_time", "triage_time", "self_service", "kb_accuracy"]
    assert {k: kpis[k]["baseline"] for k in kpis} == {"alert_noise": 32.5, "ack_time": 27.0, "triage_time": 18,
                                                      "self_service": 15.0, "kb_accuracy": 69.4}
    assert kpis["alert_noise"]["accuracy"]["n"] == 80
    assert kpis["triage_time"]["accuracy"]["n"] == 50
    assert kpis["self_service"]["accuracy"]["n"] == 1310
    assert kpis["kb_accuracy"]["accuracy"]["n"] == 30
    assert kpis["triage_time"]["accuracy"]["pct"] >= 80                     # category, curated labels
    assert kpis["triage_time"]["accuracy"]["priority_within_1_pct"] >= 90
    assert kpis["self_service"]["accuracy"]["wrongly_self_served"] == 0      # never self-serve an ineligible request
    assert kpis["ack_time"]["accuracy"]["pct"] >= 90                          # true-positive Critical alerts acknowledged
    assert all(k["kind"] == "measured" for k in res["kpis"][:4]) and kpis["kb_accuracy"]["kind"] == "modelled"


def test_excel_check_endpoint_starts_and_reports(fresh):
    from fastapi.testclient import TestClient
    from app.api.main import app
    with TestClient(app) as c:
        body = c.get("/api/kpis/excel").json()
        assert body["results"] is None and body["status"]["running"] is False
