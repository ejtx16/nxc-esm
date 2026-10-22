"""Scenario generator (docs/planning/implementation-plan.md §5): replay-driven "real time" for the demo and the KPI simulation.

licensecore_outage  47 duplicate LicenseCore alerts + related BillingEngine alerts over 22 min + background noise (D1-D3)
self_service_requests two portal requests: a low-risk password reset fulfilled from the KB and a high-risk admin-access
                    request held for a human approver; both land in the audit trail (D3, D4, D7)
sr_surge            3,200 password-reset / access requests arriving over 6 days while the SR team is diverted (D4)
normal_day          a mixed day of alerts, incident tickets and SRs at typical rates (KPI simulation)
excel_replay        the curated Excel rows over one week (Excel accuracy check, app/kpi/excel_check.py)
excel_calibrated_day normal_day's volumes with Excel-measured distributions (validation V1, app/kpi/validation.py)
json_alerts         the 4 alerts in docs/validation/data/alert-log.json: 3 duplicate noise alerts suppressed, 1 real P1 incident
"""
from __future__ import annotations

import csv
import functools
import json
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from app import settings
from app.data import db
from app.runtime.clock import clock, iso
from app.tools import mock_servicenow as snow

ALERT_NAMES = ["Failed login attempts > 50/min", "Error rate > 5% in 5 min", "Service health check failed",
               "Cache hit rate dropped below 40%", "Batch job failure", "DB connection pool exhausted",
               "Disk usage > 85%", "API response time > 5s", "CPU utilization > 90%", "Queue depth > 10,000",
               "Memory usage spike", "SSL certificate expiring in 7 days"]

TICKET_DESCRIPTIONS = [
    "Mobile app push notifications delayed by up to 6 hours", "Report generation module timing out for large date ranges",
    "Search module returning stale results after cache flush", "Bulk import of broker data failing with encoding error",
    "Dashboard KPIs showing incorrect figures post data migration", "Two-factor authentication failing for VPN-connected agents",
    "Customer history timeline not loading on CRM after upgrade", "PDF generation for policy documents failing for large policies",
    "Automated email notifications not triggering after claim approval", "Change management system not auto-closing resolved tasks",
    "Session timeout occurring after 2 mins instead of 30 mins", "Payment gateway returning duplicate transaction references",
]


@dataclass
class SEvent:
    t: float                     # seconds from scenario start (sim time)
    kind: str                    # alert | sr | incident | signal
    data: dict


@dataclass
class Scenario:
    name: str
    title: str
    description: str
    start: datetime
    duration_s: float
    default_speed: float
    events: list[SEvent] = field(default_factory=list)
    setup: Callable[[str], None] | None = None
    demo_items: str = ""

    @property
    def total(self) -> int:
        return len(self.events)


# --- generators ---------------------------------------------------------------------------------
def _systems_weighted() -> tuple[list[str], list[int]]:
    rows = db.query("SELECT source_system, total_alerts_6mo FROM alert_noise")
    return [r["source_system"] for r in rows], [r["total_alerts_6mo"] for r in rows]


def _problem_events(rng: random.Random, t0: float, system: str, name: str, severity: str, fp: bool,
                    recovered: bool, dups: int, spread_s: float) -> list[SEvent]:
    out = []
    for i in range(dups):
        t = t0 + (0 if i == 0 else rng.uniform(5, spread_s))
        out.append(SEvent(t, "alert", {"system": system, "name": name, "severity": severity, "fp": fp,
                                       "recovered": recovered}))
    return out


def _background(rng: random.Random, n_problems: int, duration_s: float, p_fp: float, allow_critical_tp: bool,
                exclude: tuple[str, ...] = ()) -> list[SEvent]:
    systems, weights = _systems_weighted()
    pool = [(s, w) for s, w in zip(systems, weights) if s not in exclude]
    out: list[SEvent] = []
    for _ in range(n_problems):
        system = rng.choices([s for s, _ in pool], [w for _, w in pool])[0]
        name = rng.choice(ALERT_NAMES)
        fp = rng.random() < p_fp
        if fp:
            severity = rng.choices(["Low", "Medium", "High", "Critical"], [0.32, 0.28, 0.32, 0.08 if allow_critical_tp else 0.0])[0]
            recovered = rng.random() < 0.8
        else:
            severity = rng.choices(["Critical", "High", "Medium", "Low"],
                                   [0.30 if allow_critical_tp else 0.0, 0.45, 0.15, 0.10])[0]
            recovered = rng.random() < 0.05
        dups = rng.choice([1, 2, 2, 3, 3, 4])
        out += _problem_events(rng, rng.uniform(5, duration_s - 60), system, name, severity, fp, recovered, dups, 240)
    return out


def _sr_event(rng: random.Random, t: float, emp: dict, kind: str, channel: str, free_text: bool = False) -> SEvent:
    ent = settings.load_yaml("entitlements")
    roles = ent["roles"]
    data = {"requestor_id": emp["employee_id"], "channel": channel, "request_type": kind, "text": None,
            "target_system": None, "access_level": None}
    if kind == "Password Reset":
        if free_text:
            data.update(request_type=None, text=rng.choice(["I'm locked out of my account, please reset my password",
                                                            "Forgot my password - can you reset it?",
                                                            "Need a password reset, can't log in"]))
    elif kind == "Access Provisioning":
        allowed = roles.get(emp["role"], {}).get("systems", [])
        roll = rng.random()
        if roll < 0.85 and allowed:
            system, level = rng.choice(allowed), "standard"
        elif roll < 0.93:
            others = [a for a in settings.load_yaml("apps")["apps"] if a not in allowed
                      and a not in ent["privileged_systems"]]
            system, level = rng.choice(others), "standard"
        else:
            system, level = rng.choice(list(settings.load_yaml("apps")["apps"])), "admin"
        data.update(target_system=system, access_level=level)
        if free_text:
            data.update(request_type=None, target_system=None, access_level=None,
                        text=f"Please give me {'admin ' if level == 'admin' else ''}access to {system}")
    return SEvent(t, "sr", data)


def _employees() -> list[dict]:
    return db.query("SELECT * FROM employees WHERE status='active' AND employee_id LIKE 'EMP%' ORDER BY employee_id")


def licensecore_outage(seed: int = 7) -> Scenario:
    rng = random.Random(seed)
    dur = 1320.0
    ev: list[SEvent] = [SEvent(0.0, "alert", {"system": "LicenseCore", "name": "Service health check failed",
                                              "severity": "Critical", "fp": False, "recovered": False})]
    for t in sorted(rng.uniform(2, dur - 5) for _ in range(46)):
        ev.append(SEvent(t, "alert", {"system": "LicenseCore", "name": "Service health check failed",
                                      "severity": rng.choice(["Critical"] * 4 + ["High"]), "fp": False, "recovered": False}))
    for t in sorted(rng.uniform(240, 900) for _ in range(6)):
        ev.append(SEvent(t, "alert", {"system": "BillingEngine", "name": "Queue depth > 10,000",
                                      "severity": rng.choice(["High", "Critical"]), "fp": False, "recovered": False}))
    ev += _background(rng, 45, dur, 0.78, allow_critical_tp=False, exclude=("LicenseCore", "BillingEngine"))
    emps = _employees()
    pool = [e for e in emps if e["employee_id"] >= "EMP7000"]
    for i in range(12):
        ev.append(_sr_event(rng, rng.uniform(40, 1250), rng.choice(pool), rng.choice(["Password Reset"] * 3 + ["Access Provisioning"]), "ticket"))
    for i in range(6):
        ev.append(_sr_event(rng, rng.uniform(60, 1200), rng.choice(pool), "Password Reset", "portal", free_text=i % 2 == 0))
    ev.append(SEvent(700, "sr", {"requestor_id": "EMP1003", "channel": "portal", "request_type": "Access Provisioning",
                                 "target_system": "BillingEngine", "access_level": "admin", "text": None}))
    ev.append(SEvent(360, "incident", {"description": TICKET_DESCRIPTIONS[0], "reported_by": "user_410@example.com"}))
    ev.append(SEvent(600, "incident", {"description": TICKET_DESCRIPTIONS[1], "reported_by": "user_233@example.com"}))

    base = settings.sim_start()                   # the case has the outage at 09:00, five minutes after "now"

    def setup(run_id: str) -> None:
        r = random.Random(seed + 1)
        pend = [e for e in _employees() if e["employee_id"] >= "EMP7000"]
        for i in range(8):                        # requests already waiting for the (now diverted) SR team
            emp = r.choice(pend)
            sid = snow.next_sr_id()
            kind = "Password Reset" if i < 6 else "Access Provisioning"
            db.insert("service_requests", {
                "sr_id": sid, "request_type": kind, "requestor_id": emp["employee_id"], "department": emp["department"],
                "submitted_ts": iso(base - timedelta(minutes=35) + timedelta(minutes=5 * i)), "priority": "Medium",
                "status": "Pending Human", "origin": "scenario", "run_id": run_id, "channel": "ticket",
                "sla_target_hrs": snow.sr_sla_target(kind),
                "target_system": None if kind == "Password Reset" else r.choice(settings.load_yaml("entitlements")["roles"][emp["role"]]["systems"]),
                "access_level": None if kind == "Password Reset" else "standard",
                "trace": db.dumps([{"ts": iso(base - timedelta(minutes=35)), "text": "Queued for the SR team (human capacity)"}])})
    return Scenario("licensecore_outage", "LicenseCore P1 outage",
                    "47 duplicate LicenseCore alerts and related BillingEngine alerts over 22 minutes, plus background noise "
                    "and SRs arriving while the team is pulled onto the P1.",
                    base + timedelta(minutes=5), dur, 22.0, sorted(ev, key=lambda e: e.t), setup, "D1, D2, D3, D4, D5")


def self_service_requests(seed: int = 0) -> Scenario:
    """Simulation 2: two employees use the self-service portal. The Guardrail classes the password reset as low risk, so
    the SR agent completes it from the knowledge document; the admin-access request is high risk and waits for a person."""
    ev = [SEvent(5.0, "sr", {"requestor_id": "EMP1002", "channel": "portal", "request_type": "Password Reset",
                             "target_system": None, "access_level": None,
                             "text": "I'm locked out of my account, please reset my password"}),
          SEvent(25.0, "sr", {"requestor_id": "EMP1003", "channel": "portal", "request_type": "Access Provisioning",
                              "target_system": "BillingEngine", "access_level": "admin",
                              "text": "Please give me admin access to BillingEngine for the month-end billing run"})]
    return Scenario("self_service_requests", "Self-service portal: two requests",
                    "Two employees submit requests on the self-service portal. The password reset passes the Guardrail as "
                    "low risk and is completed from the knowledge article; the admin-access request is high risk and "
                    "waits for a human approver. Both are recorded in the audit trail.",
                    settings.sim_start(), 60.0, 1.0, ev, None, "D3, D4, D7")


def sr_surge(seed: int = 11, n: int = 3200) -> Scenario:
    rng = random.Random(seed)
    dur = 6 * 86400.0
    emps = [e for e in _employees() if e["employee_id"] >= "EMP7000"]
    others = ["Software Installation", "Report Request", "Role Change", "New User Onboarding", "Hardware Request",
              "Data Export Request", "VPN Access Setup"]
    ev = []
    for t in sorted(rng.uniform(0, dur) for _ in range(n)):
        emp = rng.choice(emps)
        roll = rng.random()
        kind = "Password Reset" if roll < 0.70 else ("Access Provisioning" if roll < 0.94 else rng.choice(others))
        channel = "ticket" if rng.random() < 0.65 else "portal"
        ev.append(_sr_event(rng, t, emp, kind, channel))
    ev.insert(0, SEvent(0.0, "signal", {"type": "sr.surge_mode_on", "entity": "SR-SURGE", "payload": {"reason": "backlog scenario"}}))

    def setup(run_id: str) -> None:
        db.set_setting("surge_mode", True)
    return Scenario("sr_surge", "SR backlog surge (6 days, 3,200 requests)",
                    "The case-study backlog: 3,200 requests - 94% password resets and access provisioning - while the SR team is diverted.",
                    settings.sim_start(), dur, dur / 60, ev, setup, "D4, KPIs")


def normal_day(seed: int = 5) -> Scenario:
    rng = random.Random(seed)
    dur = 86400.0
    ev = _background(rng, 55, dur, 0.53, allow_critical_tp=True)
    emps = _employees()
    pool = [e for e in emps if e["employee_id"] >= "EMP7000"]
    for t in sorted(rng.uniform(0, dur) for _ in range(60)):
        kind = rng.choices(["Password Reset", "Access Provisioning", "Software Installation", "Report Request"], [0.5, 0.25, 0.15, 0.10])[0]
        ev.append(_sr_event(rng, t, rng.choice(pool), kind, rng.choice(["portal", "ticket"]), free_text=rng.random() < 0.3))
    for t in sorted(rng.uniform(0, dur) for _ in range(12)):
        ev.append(SEvent(t, "incident", {"description": rng.choice(TICKET_DESCRIPTIONS), "reported_by": f"user_{rng.randint(100, 999)}@example.com"}))
    return Scenario("normal_day", "A normal day", "Mixed alerts, incident tickets and service requests at typical rates.",
                    settings.sim_start().replace(hour=0, minute=0), dur, dur / 60, sorted(ev, key=lambda e: e.t), None, "KPIs")


def parse_ts(v: str) -> datetime:
    return datetime.strptime(v, "%Y-%m-%d %H:%M")


def excel_profile() -> dict:
    """Alert and SR distributions measured on the spreadsheets (seed rows), used by `excel_calibrated_day`.

    P(false positive | system) is the Noise Analysis Noise_Ratio_Pct; severity, alert name and self-recovery are drawn
    from the Alert Log conditioned on the true/false-positive label (add-one smoothing on names); the SR mix is the
    Automation Opportunity monthly volume."""
    seed_alerts = db.query("SELECT alert_name, severity, truth_fp, self_recovered FROM alerts WHERE origin='seed'")
    prof: dict = {"systems": {r["source_system"]: {"weight": r["total_alerts_6mo"], "p_fp": r["noise_ratio_pct"] / 100}
                              for r in db.query("SELECT * FROM alert_noise")}}
    for label, fp in (("fp", 1), ("tp", 0)):
        rows = [a for a in seed_alerts if a["truth_fp"] == fp]
        sev = {s: sum(1 for a in rows if a["severity"] == s) for s in ("Critical", "High", "Medium", "Low")}
        names = {n: 1 + sum(1 for a in rows if a["alert_name"] == n) for n in ALERT_NAMES}
        prof[label] = {"n": len(rows), "severity": sev, "names": names,
                       "p_recovered": sum(a["self_recovered"] for a in rows) / max(1, len(rows))}
    prof["sr_mix"] = {r["request_type"]: r["avg_volume_per_month"]
                      for r in db.query("SELECT request_type, avg_volume_per_month FROM automation_opportunity")}
    return prof


def excel_calibrated_day(seed: int = 5) -> Scenario:
    """`normal_day` with the same volumes and duplicate structure, but every distribution taken from the Excel files
    instead of the case narrative (validation V1, app/kpi/validation.py)."""
    rng = random.Random(seed)
    dur = 86400.0
    prof = excel_profile()
    systems = list(prof["systems"])
    ev: list[SEvent] = []
    for _ in range(55):
        system = rng.choices(systems, [prof["systems"][s]["weight"] for s in systems])[0]
        fp = rng.random() < prof["systems"][system]["p_fp"]
        p = prof["fp" if fp else "tp"]
        severity = rng.choices(list(p["severity"]), list(p["severity"].values()))[0]
        name = rng.choices(list(p["names"]), list(p["names"].values()))[0]
        recovered = rng.random() < p["p_recovered"]
        dups = rng.choice([1, 2, 2, 3, 3, 4])
        ev += _problem_events(rng, rng.uniform(5, dur - 60), system, name, severity, fp, recovered, dups, 240)
    pool = [e for e in _employees() if e["employee_id"] >= "EMP7000"]
    kinds, weights = list(prof["sr_mix"]), list(prof["sr_mix"].values())
    for t in sorted(rng.uniform(0, dur) for _ in range(round(sum(weights) / 30))):
        kind = rng.choices(kinds, weights)[0]
        ev.append(_sr_event(rng, t, rng.choice(pool), kind, rng.choice(["portal", "ticket"]),
                            free_text=kind in ("Password Reset", "Access Provisioning") and rng.random() < 0.3))
    for t in sorted(rng.uniform(0, dur) for _ in range(12)):
        ev.append(SEvent(t, "incident", {"description": rng.choice(TICKET_DESCRIPTIONS), "reported_by": f"user_{rng.randint(100, 999)}@example.com"}))
    return Scenario("excel_calibrated_day", "A normal day (Excel-calibrated)",
                    "The normal day's volumes, with alert noise, severity, self-recovery and the SR mix drawn from the "
                    "Excel files instead of the case figures.",
                    settings.sim_start().replace(hour=0, minute=0), dur, dur / 60, sorted(ev, key=lambda e: e.t), None,
                    "Validation V1")


def curated(name: str) -> list[dict]:
    """Rows of data/curated/<name>_expected.csv (the Excel-derived answer key)."""
    with (settings.CURATED_DIR / f"{name}_expected.csv").open() as fh:
        return list(csv.DictReader(fh))


def excel_replay(seed: int = 0) -> Scenario:
    """The Excel rows themselves, in their original order, spread over one simulated week. Each event carries the
    curated row id as `ref` so the Excel accuracy check can grade the agent decision (the player ignores it)."""
    dur = 5 * 86400.0

    def spread(rows: list[dict], offset: float) -> list[tuple[float, dict]]:
        step = (dur - 3600) / max(1, len(rows))
        return [(offset + i * step, r) for i, r in enumerate(rows)]

    ev = [SEvent(t, "alert", {"ref": r["id"], "system": r["source_system"], "name": r["alert_name"],
                              "severity": r["severity"], "fp": r["expected_noise"] == "Y",
                              "recovered": r["self_recovered"] == "Y"})
          for t, r in spread(curated("alerts"), 60)]
    ev += [SEvent(t, "incident", {"ref": r["id"], "description": r["description"], "reported_by": r["reported_by"]})
           for t, r in spread(curated("incidents"), 600)]
    ev += [SEvent(t, "sr", {"ref": r["id"], "requestor_id": r["requestor_id"], "channel": r["channel"],
                            "request_type": r["request_type"], "text": None, "target_system": r["target_system"] or None,
                            "access_level": r["access_level"] or None})
           for t, r in spread(curated("srs"), 30)]
    start = settings.sim_start().replace(hour=0, minute=0) + timedelta(days=2)
    return Scenario("excel_replay", "Excel data replay (accuracy check)",
                    "The 80 logged alerts, 50 logged incidents and one month of service requests at the logged volumes, "
                    "graded against the curated answer key in data/curated.",
                    start, dur, dur / 60, sorted(ev, key=lambda e: e.t), None, "KPIs")


def alert_noise_sim(seed: int = 0) -> Scenario:
    """The simulated alert stream (data/curated/alerts_simulated_expected.csv), replayed at its own timestamps."""
    rows = curated("alerts_simulated")
    t0 = parse_ts(rows[0]["alert_ts"])
    ev = [SEvent((parse_ts(r["alert_ts"]) - t0).total_seconds() + 60,
                 "alert", {"ref": r["id"], "system": r["source_system"], "name": r["alert_name"],
                           "severity": r["severity"], "fp": r["expected_noise"] == "Y",
                           "recovered": r["self_recovered"] == "Y"}) for r in rows]
    dur = max(e.t for e in ev) + 3600
    start = settings.sim_start().replace(hour=0, minute=0) + timedelta(days=2)
    return Scenario("alert_noise_sim", "Simulated alert stream (noise check)",
                    "Simulated alerts with flapping noise and real incidents, graded against known labels.",
                    start, dur, dur / 60, sorted(ev, key=lambda e: e.t), None, "KPIs")


ALERT_LOG = settings.REPO_DIR / "docs" / "validation" / "data" / "alert-log.json"


def json_alerts(seed: int = 0) -> Scenario:
    """docs/validation/data/alert-log.json replayed in timestamp order. Gaps longer than the dedupe window shrink to 2 minutes so the
    months-apart rows play in a few minutes while repeats of one alert stay inside the window."""
    rows = sorted(json.loads(ALERT_LOG.read_text())["alerts"], key=lambda r: r["Alert_Timestamp"])
    window = settings.cfg()["alert"]["dedupe_window_s"]
    ev, t, prev = [], 60.0, None
    for r in rows:
        ts = datetime.fromisoformat(r["Alert_Timestamp"])
        if prev is not None:
            gap = (ts - prev).total_seconds()
            t += gap if gap <= window else 120.0
        prev = ts
        ev.append(SEvent(t, "alert", {"ref": r["Alert_ID"], "system": r["Source_System"], "name": r["Alert_Name"],
                                      "severity": r["Severity"], "fp": not r["True_Positive"],
                                      # cleared on its own: auto-resolved without an incident being raised
                                      "recovered": bool(r["Auto_Resolved"] and not r["Incident_Created"])}))
    # Mid-month, so the month-end change freeze does not apply.
    start = settings.sim_start().replace(day=15, hour=9, minute=0, second=0, microsecond=0)
    return Scenario("json_alerts", "Alert log",
                    f"The {len(ev)} alerts in the alert log: the repeated noise alert is deduped and suppressed, "
                    "and the one real Critical alert becomes a P1 incident that the Guardrail hands to a person.",
                    start, t + 3600, 60.0, ev, None, "D1-D3")


BUILDERS: dict[str, Callable[..., Scenario]] = {
    "json_alerts": json_alerts, "licensecore_outage": licensecore_outage, "self_service_requests": self_service_requests,
    "sr_surge": sr_surge, "normal_day": normal_day,
    "excel_replay": excel_replay, "alert_noise_sim": alert_noise_sim, "excel_calibrated_day": excel_calibrated_day}


def build(name: str, seed: int | None = None) -> Scenario:
    if name not in BUILDERS:
        raise KeyError(f"unknown scenario '{name}'")
    return BUILDERS[name](seed) if seed is not None else BUILDERS[name]()


@functools.cache
def title(name: str) -> str:
    """Display name for a scenario id; building one is cheap enough to do once per name."""
    return BUILDERS[name]().title if name in BUILDERS else name


def catalogue() -> list[dict]:
    return [{"name": n, **{k: getattr(s, k) for k in ("title", "description", "duration_s", "default_speed", "demo_items")},
             "events": s.total} for n, s in ((n, BUILDERS[n]()) for n in BUILDERS)]
