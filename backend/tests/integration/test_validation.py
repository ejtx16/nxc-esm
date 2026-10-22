"""Validation scenarios V1-V3 (docs/validation/validation-scenarios.md): the checks that the KPI results hold up."""
from datetime import datetime, timedelta

import pytest

from app.data import db
from app.kpi import validation


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    db.configure(tmp_path_factory.mktemp("val") / "validation.db")
    return validation.run_validation()


def test_v1_excel_calibrated_day_reproduces_the_excel_distributions(results):
    v1 = results["v1_excel_calibrated"]
    excel, case = v1["arms"]["excel_calibrated_day"], v1["arms"]["normal_day"]
    assert 20 <= excel["raw_noise_pct"] <= 45                        # Excel: 32.5% (Alert Log), 32.1% (Noise Analysis)
    assert 45 <= case["raw_noise_pct"] <= 60                         # case: 53%
    assert excel["excel_profile"]["critical_that_are_fp_pct"] == 41.9
    assert v1["noise_reduction_pts"]["normal_day"] >= 10                # the case-calibrated day: the agent works
    assert set(v1["noise_reduction_pts"]) == {"normal_day", "excel_calibrated_day"}
    assert v1["reading"]                                             # reported either way, never gated


def test_v2_as_is_model_is_calibrated_to_the_case_and_agents_beat_it(results):
    v2 = results["v2_human_baseline"]
    surge = v2["sr_surge"]
    assert abs(surge["as_is"]["breach_pct"] - 84.0) <= 1.0           # calibrated to the case's 84% SR breach
    assert 0 < surge["calibrated_diversion_share"] < 1
    assert surge["to_be"]["breach_pct"] < surge["as_is"]["breach_pct"]
    assert surge["to_be"]["human_handled"] + surge["to_be"]["agent_completed"] == surge["srs"] == 3200
    outage = v2["p1_outage"]
    assert outage["surfaced_clusters"] < outage["raw_alerts"]
    assert outage["people_min_freed"] > 0


def test_v3_sensitivity_matches_the_kpi_simulation_at_default_and_degrades_monotonically(results):
    v3 = results["v3_sensitivity"]
    assert v3["incidents"] == 22
    assert v3["at_default"]["mttr_h"] == 6.1 and v3["at_default"]["sla_compliance_pct"] == 90.9
    rows = v3["one_at_a_time"]["runbook_success_pct"]
    assert [r["value"] for r in rows] == sorted((r["value"] for r in rows), reverse=True)
    assert all(a["mttr_h"] <= b["mttr_h"] and a["sla_compliance_pct"] >= b["sla_compliance_pct"]
               for a, b in zip(rows, rows[1:]))
    assert len(v3["grid"]) == len(validation.SWEEP["runbook_success_pct"]) * len(validation.SWEEP["approval_latency_h"])


def test_human_queue_is_fifo_at_the_given_rate():
    t0 = datetime(2025, 8, 4, 9, 0)
    items = [{"submitted": t0 + timedelta(minutes=m), "target_h": 0.75} for m in (0, 0, 30)]
    done = [i["done"] for i in validation.human_queue(items, 2.0, t0)]      # 2 per hour = 30 min each
    assert done == [t0 + timedelta(minutes=30), t0 + timedelta(minutes=60), t0 + timedelta(minutes=90)]
    assert validation._breach_pct(validation.human_queue(items, 2.0, t0), t0 + timedelta(hours=3)) == pytest.approx(66.7)   # 2nd and 3rd wait 1 h
