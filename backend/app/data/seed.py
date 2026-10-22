"""Seed SQLite from the dummy spreadsheets + YAML config (`python -m app.data.seed --reset`).

The spreadsheets are random dummy data, so they are treated as SEED data, not ground truth
(docs/planning/implementation-plan.md §5): Outdated_Flag is ignored, KB bodies are synthesised, and the
alert/SR scenarios come from app/data/scenarios.py.
"""
from __future__ import annotations

import argparse
import random
import re
from datetime import datetime

import pandas as pd

from app import settings
from app.data import db

PRIORITY_OF_SEVERITY = {"Critical": "P1", "High": "P2", "Medium": "P3", "Low": "P4"}

DEPARTMENT_ROLE = {
    "IT": "Engineer", "Sales": "Agent", "Finance": "Finance", "Legal": "Analyst", "Compliance": "Analyst",
    "Operations": "Support", "HR": "Manager", "Customer Success": "Support", "Product Engineering": "Engineer",
}
FIRST = ["Dana", "Morgan", "Riley", "Jamie", "Avery", "Quinn", "Parker", "Rowan", "Skyler", "Taylor", "Devon",
         "Harper", "Reese", "Emerson", "Finley", "Kendall"]
LAST = ["Nguyen", "Okafor", "Silva", "Kowalski", "Haddad", "Larsen", "Mehta", "Castillo", "Brennan", "Tanaka"]


# --- KB body synthesis --------------------------------------------------------------------
# {admin} {runbooks} {platform} {dash} {sso} {assign} resolve to a DEPRECATED path or its current equivalent.
PLACEHOLDER_KEYS = {
    "admin": "LicenseCore v2 admin console", "runbooks": "\\\\nexa-fs01\\runbooks", "platform": "NexaStack v3",
    "dash": "Splunk classic dashboards", "sso": "legacy SSO gateway", "assign": "manual round-robin assignment",
}
TOPIC_STEPS: list[tuple[tuple[str, ...], list[str]]] = [
    (("password",), ["Verify the requester's identity (employee ID and manager confirmation for contractors).",
                     "Sign in to the {sso} admin page and locate the user account.",
                     "Trigger a password reset and deliver the temporary credential over the secure channel.",
                     "Confirm the user can sign in and is forced to change the password at first login."]),
    (("licensecore",), ["Open the {admin} and check LicenseCore service health.",
                        "Review API latency and queue depth for the licence worker pool.",
                        "If latency stays above 5s, restart the licence worker pool as described in {runbooks}.",
                        "Validate renewals are processing and record the outcome on the incident."]),
    (("billingengine", "billing"), ["Look up the error code in the reference table for {platform}.",
                                    "Check the billing queue depth and the last successful invoice run.",
                                    "Reprocess failed invoices using the documented replay procedure in {runbooks}.",
                                    "Escalate to App Support L2 if the same code recurs within 30 minutes."]),
    (("integrationhub", "api"), ["Check the failing route in the {dash} and capture the correlation ID.",
                                 "Verify upstream and downstream endpoint health.",
                                 "Apply the retry/back-off policy; do not replay non-idempotent calls.",
                                 "Raise an incident for the owning team if errors persist after 3 retries."]),
    (("vpn",), ["Confirm the user's device is enrolled and the VPN client is on the supported version.",
                "Validate the user's MFA registration in the {sso}.",
                "Re-issue the VPN profile and test the tunnel.", "Log the resolution on the SR."]),
    (("onboarding", "access provisioning"), ["Collect the HR record and manager approval for the new joiner.",
                                              "Create the standard access bundle for the role; privileged access needs L2 approval.",
                                              "Use {assign} only when the skills-based queue is unavailable.",
                                              "Confirm the joiner can sign in and close the SR."]),
    (("p1", "incident"), ["Declare the P1 and open the bridge; assign an incident commander.",
                          "Post an update every 15 minutes in the status channel.",
                          "Escalate using the on-call rota described in {runbooks}; avoid {assign}.",
                          "Run the post-incident review within 3 working days."]),
    (("mfa", "certificate", "ssl"), ["Check the certificate / enrolment status in the {sso}.",
                                      "Renew or re-enrol using the documented procedure.",
                                      "Verify dependent services after the change.", "Record the renewal date in {platform} inventory."]),
    (("batch", "monitoring", "report", "export"), ["Open the job history in the {dash}.",
                                                   "Identify the failed step and its input window.",
                                                   "Re-run the job for the failed window only.", "Confirm outputs and notify the requester."]),
]
GENERIC_STEPS = ["Confirm the symptom and the affected users.", "Check the service status in the {dash}.",
                 "Apply the documented fix; capture screenshots for the ticket.",
                 "Escalate to the owning team if not resolved within the SLA window."]


def _systems_for(title: str) -> list[str]:
    apps = settings.load_yaml("apps")["apps"]
    t = title.lower()
    hits = [a for a in apps if a.lower() in t]
    if "password" in t or "mfa" in t:
        hits.append("AuthService")
    if "mobile" in t:
        hits.append("CustomerPortal")
    return sorted(set(hits))


def build_kb_body(row: dict) -> str:
    title = row["Title"]
    lowered = title.lower()
    steps = GENERIC_STEPS
    for keys, topic_steps in TOPIC_STEPS:
        if any(k in lowered for k in keys):
            steps = topic_steps
            break
    rng = random.Random(row["Article_ID"])
    last = datetime.fromisoformat(str(row["Last_Updated"]))
    stale_days = (settings.CASE_START - last).days   # relative to the case, before the date shift
    p = min(0.9, max(0.1, (stale_days - 90) / 700))
    used = sorted(set(re.findall(r"\{(\w+)\}", " ".join(steps))))
    force = "licensecore" in lowered          # the LicenseCore runbooks are the stale ones used in the P1
    deprecated = {k for k in used if force or rng.random() < p}
    current = settings.cfg()["kb"]["deprecated_paths"]

    def resolve(match: re.Match) -> str:
        key = match.group(1)
        old = PLACEHOLDER_KEYS[key]
        return old if key in deprecated else current[old]

    lines = [f"# {title}", f"Category: {row['Category']}", f"Owner: {row['Author']}", "", "Procedure:"]
    for i, step in enumerate(steps, 1):
        lines.append(f"{i}. " + re.sub(r"\{(\w+)\}", resolve, step))
    return "\n".join(lines)


# --- loaders ------------------------------------------------------------------------------
def _shift(v):
    """Move a historic date / datetime by the case-to-today offset so record ages match the local sim date."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return v
    delta = settings.date_shift()
    text = str(v)
    dt = datetime.fromisoformat(text) + delta
    dt = min(dt, settings.sim_start())            # the dummy data runs past the case date; nothing is dated in the future
    return dt.strftime("%Y-%m-%d") if len(text) <= 10 else dt.strftime("%Y-%m-%d %H:%M")


def _snake(cols) -> list[str]:
    return [c.strip().lower() for c in cols]


def _guess_app(text: str) -> str | None:
    t = text.lower()
    best, best_hits = None, 0
    for app, kws in settings.load_yaml("apps")["keywords"].items():
        hits = sum(1 for k in kws if k in t)
        if hits > best_hits:
            best, best_hits = app, hits
    return best


def load_spreadsheets() -> dict[str, int]:
    f = settings.FILES_DIR
    counts: dict[str, int] = {}
    sla_hours = settings.cfg()["sla_hours"]

    inc = pd.read_excel(f / "incident_log.xlsx", sheet_name="Incident Log")
    inc.columns = _snake(inc.columns)
    rows = []
    for r in inc.to_dict("records"):
        r["reported_date"] = _shift(r["reported_date"])
        rows.append({**r, "origin": "seed", "app_id": _guess_app(r["short_description"]),
                     "sla_hours": sla_hours.get(r["priority"]), "opened_ts": r["reported_date"],
                     "escalated": 0, "held": 0, "triage_mode": None})
    with db.tx():
        for r in rows:
            db.insert("incidents", r)
    counts["incidents"] = len(rows)

    sla = pd.read_excel(f / "incident_log.xlsx", sheet_name="SLA Summary")
    sla.columns = _snake(sla.columns)
    with db.tx():
        for r in sla.to_dict("records"):
            db.insert("sla_summary", r)

    alerts = pd.read_excel(f / "monitoring_alerts.xlsx", sheet_name="Alert Log")
    alerts.columns = _snake(alerts.columns)
    with db.tx():
        for r in alerts.to_dict("records"):
            r = {k: (None if pd.isna(v) else v) for k, v in r.items()}
            r["alert_ts"] = _shift(r.pop("alert_timestamp"))
            r.update(origin="seed", status="historic", truth_fp=1 if r["true_positive"] == "No" else 0,
                     self_recovered=1 if r["auto_resolved"] == "Yes" else 0)
            db.insert("alerts", r)
    counts["alerts"] = len(alerts)

    noise = pd.read_excel(f / "monitoring_alerts.xlsx", sheet_name="Alert Noise Analysis")
    noise.columns = _snake(noise.columns)
    with db.tx():
        for r in noise.to_dict("records"):
            db.insert("alert_noise", r)

    sr = pd.read_excel(f / "service_request_log.xlsx", sheet_name="Service Requests")
    sr.columns = _snake(sr.columns)
    with db.tx():
        for r in sr.to_dict("records"):
            db.insert("service_requests", {
                "sr_id": r["sr_id"], "request_type": r["request_type"], "requestor_id": r["requestor_id"],
                "department": r["department"], "submitted_ts": _shift(r["submitted_date"]), "priority": r["priority"],
                "status": r["status"], "fulfillment_time_hrs": r["fulfillment_time_hrs"],
                "sla_target_hrs": r["sla_target_hrs"], "sla_met": r["sla_met"], "fulfilled_by": r["fulfilled_by"],
                "manual_steps": r["manual_steps"], "automation_potential": r["automation_potential"],
                "origin": "seed", "channel": "ticket"})
    counts["service_requests"] = len(sr)

    auto = pd.read_excel(f / "service_request_log.xlsx", sheet_name="Automation Opportunity")
    auto.columns = _snake(auto.columns)
    with db.tx():
        for r in auto.to_dict("records"):
            db.insert("automation_opportunity", r)

    kb = pd.read_excel(f / "kb_article_quality.xlsx")
    with db.tx():
        for r in kb.to_dict("records"):
            db.insert("kb_articles", {
                "article_id": r["Article_ID"], "title": r["Title"], "category": r["Category"],
                "created_date": _shift(r["Created_Date"]), "last_updated": _shift(r["Last_Updated"]), "author": r["Author"],
                "accuracy_score_pct": r["Accuracy_Score_Pct"], "completeness_score_pct": r["Completeness_Score_Pct"],
                "usage_count_90days": r["Usage_Count_90Days"], "resolved_with_kb_count": r["Resolved_With_KB_Count"],
                "escalation_despite_kb": r["Escalation_Despite_KB"], "outdated_flag": r["Outdated_Flag"],
                "recommended_action_seed": r["Recommended_Action"], "body": build_kb_body(r),
                "systems": db.dumps(_systems_for(r["Title"])), "updated_by_agent": 0})
    counts["kb_articles"] = len(kb)
    return counts


def load_apps() -> int:
    apps = settings.load_yaml("apps")["apps"]
    with db.tx():
        for app_id, a in apps.items():
            db.insert("apps", {"app_id": app_id, "name": app_id, "tier": a["tier"], "owner_team": a["owner_team"],
                               "users_affected": a["users_affected"], "depends_on": db.dumps(a["depends_on"]),
                               "description": a["description"]})
    return len(apps)


def load_employees() -> int:
    rng = random.Random(42)
    sr = db.query("SELECT DISTINCT requestor_id, department FROM service_requests")
    emp: dict[str, dict] = {}
    for r in sr:
        emp[r["requestor_id"]] = {"employee_id": r["requestor_id"], "department": r["department"]}
    for i in range(400):                                       # directory used by the SR surge scenario
        eid = f"EMP{7000 + i}"
        emp[eid] = {"employee_id": eid, "department": rng.choice(list(DEPARTMENT_ROLE))}
    demo = [("EMP1001", "Dana Nguyen", "Finance", "Finance"), ("EMP1002", "Morgan Okafor", "Agent", "Sales"),
            ("EMP1003", "Riley Silva", "Engineer", "IT"), ("EMP1004", "Jamie Larsen", "Support", "Customer Success"),
            ("EMP1005", "Avery Mehta", "Analyst", "Compliance")]
    for eid, name, role, dept in demo:
        emp[eid] = {"employee_id": eid, "name": name, "role": role, "department": dept}
    with db.tx():
        for eid, e in emp.items():
            name = e.get("name") or f"{rng.choice(FIRST)} {rng.choice(LAST)}"
            role = e.get("role") or DEPARTMENT_ROLE.get(e["department"], "Support")
            db.insert("employees", {"employee_id": eid, "name": name, "role": role, "department": e["department"],
                                    "status": "active", "email": f"{eid.lower()}@nexacore.com"})
        db.insert("employees", {"employee_id": "EMP9999", "name": "Casey Leaver", "role": "Agent",
                                "department": "Sales", "status": "terminated", "email": "emp9999@nexacore.com"})
    return len(emp) + 1


def run(reset: bool = True, score_kb: bool = True) -> dict:
    if reset:
        db.reset_schema()
    counts = load_spreadsheets()
    counts["apps"] = load_apps()
    counts["employees"] = load_employees()
    db.set_setting("surge_mode", False)
    if score_kb:
        from app.runtime import bus, engine
        bus.publish("kb.review", "ALL", {"reason": "initial_scoring", "rules": True})   # instant; Claude reviews on demand
        engine.process_pending()
    return counts


def main() -> None:
    ap = argparse.ArgumentParser(description="Seed the NexaCore prototype database")
    ap.add_argument("--reset", action="store_true", help="drop and rebuild all tables")
    ap.add_argument("--no-kb-score", action="store_true", help="skip the initial KB scoring pass")
    args = ap.parse_args()
    db.configure()
    counts = run(reset=args.reset, score_kb=not args.no_kb_score)
    print(f"Seeded {db.db_file()}: {counts}")


if __name__ == "__main__":
    main()
