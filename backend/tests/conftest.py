"""Shared fixtures: a seeded template DB is copied per test so tests are isolated and fast."""
from __future__ import annotations

import os
import shutil

import pytest

os.environ["LLM_MODE"] = "rules"
os.environ["NEXA_SKIP_KPI"] = "1"
os.environ["NEXA_SIM_START"] = "2025-08-04T08:55:00"     # tests run on the case calendar

from app.data import db, seed  # noqa: E402
from app.llm import client as llm  # noqa: E402
from app.runtime import engine, player  # noqa: E402
from app.runtime.clock import clock  # noqa: E402


@pytest.fixture(scope="session")
def template_db(tmp_path_factory):
    path = tmp_path_factory.mktemp("tpl") / "template.db"
    db.configure(path)
    seed.run(reset=True)
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return path


@pytest.fixture
def fresh(template_db, tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_MODE", "rules")
    target = tmp_path / "test.db"
    shutil.copy(template_db, target)
    db.configure(target)
    clock.reset()
    engine.reset_state()
    llm.set_client(None)
    monkeypatch.setattr(llm, "_sleep", lambda s: None)      # no real backoff between audited retries
    player.Player.current = None
    yield db
    llm.set_client(None)


def make_alert(system="LicenseCore", name="Service health check failed", severity="Critical", ts="2025-08-04T09:00:00",
               fp=False, recovered=False, run_id="T1") -> str:
    from app.tools import mock_servicenow as snow
    aid = snow.next_alert_id()
    db.insert("alerts", {"alert_id": aid, "alert_name": name, "source_system": system, "severity": severity,
                         "alert_ts": ts, "acknowledged": "No", "true_positive": "No" if fp else "Yes",
                         "incident_created": "No", "auto_resolved": "No", "origin": "scenario", "run_id": run_id,
                         "status": "new", "truth_fp": 1 if fp else 0, "self_recovered": 1 if recovered else 0})
    return aid


def send_alert(**kw) -> str:
    from app.runtime import bus
    aid = make_alert(**kw)
    bus.publish(bus.ALERT_RECEIVED, aid, {}, ts=kw.get("ts", "2025-08-04T09:00:00"), run_id=kw.get("run_id", "T1"))
    engine.process_pending()
    return aid


def make_sr(emp="EMP1002", rtype="Password Reset", channel="portal", system=None, level=None, text=None) -> str:
    from app.runtime import bus
    from app.tools import mock_servicenow as snow
    sid = snow.next_sr_id()
    db.insert("service_requests", {"sr_id": sid, "request_type": rtype, "requestor_id": emp, "submitted_ts": clock.now_iso(),
                                   "priority": "Medium", "status": "New", "origin": "live", "channel": channel,
                                   "target_system": system, "access_level": level, "text": text})
    bus.publish(bus.SR_SUBMITTED, sid, {})
    engine.process_pending()
    return sid


@pytest.fixture(scope="session")
def kpi_results(tmp_path_factory):
    """Run the full KPI simulation once per test session (isolated DB, rules mode)."""
    from app.kpi import simulate
    db.configure(tmp_path_factory.mktemp("kpi") / "kpi.db")
    return simulate.run_simulation()
