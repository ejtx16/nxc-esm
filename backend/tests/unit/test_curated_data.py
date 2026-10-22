"""The curated answer key (data/curated) covers every Excel row and leaves the spreadsheet baselines intact."""
import pandas as pd
import pytest

from app import settings
from app.data import scenarios
from app.kpi import excel_check

SOURCES = [("alerts", "monitoring_alerts.xlsx", "Alert Log", "Alert_ID"),
           ("incidents", "incident_log.xlsx", "Incident Log", "Incident_ID"),
           ("kb", "kb_article_quality.xlsx", 0, "Article_ID")]


@pytest.mark.parametrize("name,book,sheet,col", SOURCES)
def test_every_excel_row_appears_once(name, book, sheet, col):
    ids = [r["id"] for r in scenarios.curated(name)]
    excel = pd.read_excel(settings.FILES_DIR / book, sheet_name=sheet)[col].tolist()
    assert sorted(ids) == sorted(excel)


def test_service_requests_contain_the_log_and_the_monthly_volumes():
    rows = scenarios.curated("srs")
    log = pd.read_excel(settings.FILES_DIR / "service_request_log.xlsx", sheet_name="Service Requests")
    vol = pd.read_excel(settings.FILES_DIR / "service_request_log.xlsx", sheet_name="Automation Opportunity")
    assert sorted(r["id"] for r in rows if r["source"] == "log") == sorted(log["SR_ID"])
    assert len(rows) == vol["Avg_Volume_Per_Month"].sum()
    assert len({r["id"] for r in rows}) == len(rows)


@pytest.mark.parametrize("name", ["alerts", "incidents", "srs", "kb"])
def test_every_relabelled_row_has_a_reason(name):
    assert all(r["reason"].strip() for r in scenarios.curated(name) if r["changed"] == "Y")


def test_baselines_are_the_spreadsheet_figures():
    b = {k: v for k, (v, _) in excel_check.excel_baselines().items()}
    assert b == {"alert_noise": 32.5, "ack_time": 27.0, "triage_time": 18, "self_service": 15.0, "kb_accuracy": 69.4}
