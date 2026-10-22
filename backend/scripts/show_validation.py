"""Print one validation scenario from data/validation_results.json (docs/validation/validation-scenarios.md).

    .venv/bin/python scripts/show_validation.py v1|v2|v3

Run `.venv/bin/python -m app.kpi.validation` first.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

RESULTS = Path(__file__).resolve().parents[1] / "data" / "validation_results.json"


def v1(d: dict) -> None:
    v = d["v1_excel_calibrated"]
    for name, a in v["arms"].items():
        print(f"{name:22s} raw noise {a['raw_noise_pct']:5.1f}%  dedup-only {a['noise_if_nothing_suppressed_pct']:5.1f}%  "
              f"after agent {a['noise_after_pct']:5.1f}%  precision {a['suppression_precision_pct']:5.1f}%  "
              f"real wrongly suppressed {a['true_positives_wrongly_suppressed']}  self-service {a['sr_self_service_pct']}%")
    print("noise reduction (pts):", v["noise_reduction_pts"])
    print("reading:", v["reading"])


def v2(d: dict) -> None:
    o, s = d["v2_human_baseline"]["p1_outage"], d["v2_human_baseline"]["sr_surge"]
    print(f"P1 window people-minutes  AS-IS {sum(o['people_min_as_is'].values())}  "
          f"TO-BE {sum(o['people_min_to_be'].values())}  freed {o['people_min_freed']}")
    print(f"P1 ack  AS-IS {o['p1_ack_min_as_is']} min  TO-BE {o['p1_ack_min_to_be']} min")
    print(f"SR surge  calibrated diversion {s['calibrated_diversion_share']}  human capacity {s['human_rate_per_day']}/day")
    print(f"  AS-IS breach {s['as_is']['breach_pct']}%  backlog {s['as_is']['backlog_at_end']}")
    print(f"  TO-BE breach {s['to_be']['breach_pct']}%  backlog {s['to_be']['backlog_at_end']}  "
          f"people-hours freed {s['people_hours_freed']}")


def v3(d: dict) -> None:
    v = d["v3_sensitivity"]
    print("runbook success  MTTR h  SLA %  cost $M")
    for r in v["one_at_a_time"]["runbook_success_pct"]:
        print(f"  {r['value']:6.0f}%       {r['mttr_h']:5.1f}  {r['sla_compliance_pct']:5.1f}  {r['cost_m']:5.1f}")
    print("approval latency h -> SLA %:", [(r["value"], r["sla_compliance_pct"]) for r in v["one_at_a_time"]["approval_latency_h"]])
    print("break-even runbook success:", v["break_even_runbook_success_pct"])


if __name__ == "__main__":
    part = sys.argv[1] if len(sys.argv) > 1 else ""
    if part not in ("v1", "v2", "v3"):
        sys.exit(__doc__)
    {"v1": v1, "v2": v2, "v3": v3}[part](json.loads(RESULTS.read_text()))
