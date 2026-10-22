"""Build data/curated/alerts_simulated_expected.csv: a SIMULATED alert stream with realistic noise structure.

    .venv/bin/python scripts/build_simulated_alerts.py

The 80 logged alerts have labels that do not follow any visible pattern, so they cannot show whether the noise logic
works. This stream does: noise comes from flapping signatures on chatty systems and mostly self-recovers; real alerts
are mostly High/Critical and stay open. About 12% of rows deliberately break the pattern so accuracy is not 100% by
construction. It is synthetic. Quote it as a simulation, never as a measurement of NexaCore's real alerts.
"""
from __future__ import annotations

import csv
import random
from datetime import datetime, timedelta
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "data" / "curated" / "alerts_simulated_expected.csv"
SEED, N = 11, 300
START = datetime(2025, 2, 3, 0, 0)

NOISY_NAMES = {"Failed login attempts > 50/min": 5, "Batch job failure": 3, "CPU utilization > 90%": 3,
               "DB connection pool exhausted": 3, "Queue depth > 10,000": 2, "Service health check failed": 2}
REAL_NAMES = {"Disk usage > 85%": 4, "Memory usage spike": 3, "Error rate > 5% in 5 min": 5,
              "Cache hit rate dropped below 40%": 2, "API response time > 5s": 3, "SSL certificate expiring in 7 days": 1}
NOISY_SYS = {"BillingEngine": 4, "IntegrationHub": 3, "DataWarehouse": 3, "APIGateway": 2}
QUIET_SYS = {"ReportingPlatform": 3, "AuthService": 3, "LicenseCore": 3, "WorkflowEngine": 2, "CustomerPortal": 2,
             "NotificationService": 2}


def pick(rng: random.Random, weights: dict[str, int]) -> str:
    return rng.choices(list(weights), list(weights.values()))[0]


def make(rng: random.Random, noise: bool, anomalous: bool) -> dict:
    pattern_noise = noise != anomalous                 # anomalous rows look like the opposite class
    if pattern_noise:
        sev = rng.choices(["Low", "Medium", "High", "Critical"], [4, 4, 3, 1])[0]
        return {"source_system": pick(rng, NOISY_SYS if rng.random() < 0.8 else QUIET_SYS),
                "alert_name": pick(rng, NOISY_NAMES), "severity": sev, "self_recovered": "Y" if rng.random() < 0.85 else "N"}
    sev = rng.choices(["Low", "Medium", "High", "Critical"], [1, 2, 5, 5])[0]
    return {"source_system": pick(rng, QUIET_SYS if rng.random() < 0.8 else NOISY_SYS),
            "alert_name": pick(rng, REAL_NAMES), "severity": sev, "self_recovered": "Y" if rng.random() < 0.08 else "N"}


def main() -> None:
    rng = random.Random(SEED)
    rows, t, i = [], START, 0
    while len(rows) < N:
        noise = rng.random() < 0.38
        anomalous = rng.random() < 0.12
        base = make(rng, noise, anomalous)
        burst = rng.choice([1, 1, 2, 3]) if noise else 1    # flapping: the same alert repeats within a minute or two
        for k in range(burst):
            if len(rows) >= N:
                break
            ts = t + timedelta(seconds=75 * k)
            i += 1
            rows.append({"id": f"SIM{5000 + i}", "alert_ts": ts.strftime("%Y-%m-%d %H:%M"), **base,
                         "excel_label": "", "expected_noise": "Y" if noise else "N", "changed": "N",
                         "reason": "simulated" + (" (pattern-breaking)" if anomalous else "")})
        t += timedelta(minutes=rng.randint(8, 30))
    with OUT.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"{OUT.name}: {len(rows)} rows, {sum(r['expected_noise'] == 'Y' for r in rows)} noise")


if __name__ == "__main__":
    main()
