"""Build the curated, Excel-derived answer key in data/curated/ (run once; the CSVs are checked in).

    .venv/bin/python scripts/build_curated.py

Every Excel row is kept. `expected_*` is the reviewed label the Excel accuracy check grades the agents against;
`changed=Y` marks rows where it differs from the spreadsheet label, with the reason. See data/curated/README.md.
"""
from __future__ import annotations

import csv
import os
import random
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
os.environ["NEXA_DB_PATH"] = str(Path(tempfile.mkdtemp()) / "curate.db")   # never touch the live database

import pandas as pd  # noqa: E402

from app import settings  # noqa: E402
from app.data import db, seed  # noqa: E402

OUT = settings.DATA_DIR / "curated"

# Reviewed triage labels for the 10 Excel descriptions that are not in data/golden/triage_labels.csv.
# The Excel log gives each description two different random categories, so it cannot be the answer key.
EXTRA_TRIAGE = {
    "Automated email notifications not triggering after claim approval": ("Application Error", "P3"),
    "Bulk import of broker data failing with encoding error": ("Integration Failure", "P3"),
    "Change management system not auto-closing resolved tasks": ("Application Error", "P4"),
    "Customer history timeline not loading on CRM after upgrade": ("Application Error", "P3"),
    "Customer onboarding workflow stuck at document verification step": ("Application Error", "P2"),
    "Dashboard KPIs showing incorrect figures post data migration": ("Data Discrepancy", "P3"),
    "PDF generation for policy documents failing for large policies": ("Application Error", "P3"),
    "Policy endorsement screen throws null pointer exception on save": ("Application Error", "P2"),
    "Two-factor authentication failing for VPN-connected agents": ("Access Issue", "P2"),
    "Underwriting scoring module returning out-of-range values": ("Data Discrepancy", "P2"),
}

# One month of requests at the Automation Opportunity volumes. Access requests: 85% in-role, 10% out-of-role,
# 5% admin (the same mix the scenario generator uses). Self-service catalogue items arrive via the portal.
ACCESS_MIX = (0.85, 0.10, 0.05)
SELF_SERVICE_TYPES = ("Password Reset", "Access Provisioning")


def _write(name: str, rows: list[dict]) -> None:
    with (OUT / name).open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"{name}: {len(rows)} rows, {sum(r['changed'] == 'Y' for r in rows)} changed")


def alerts() -> None:
    a = pd.read_excel(settings.FILES_DIR / "monitoring_alerts.xlsx", sheet_name="Alert Log")
    rows = []
    for r in a.sort_values(["Alert_Timestamp", "Alert_ID"]).to_dict("records"):
        noise = "Y" if r["True_Positive"] == "No" else "N"
        # The labels agree with the resolution notes (every "False positive - suppressed" row and every auto-healed
        # row without an incident is True_Positive=No), so no row is overridden.
        rows.append({"id": r["Alert_ID"], "alert_ts": r["Alert_Timestamp"], "source_system": r["Source_System"],
                     "alert_name": r["Alert_Name"], "severity": r["Severity"],
                     "self_recovered": "Y" if r["Auto_Resolved"] == "Yes" else "N",
                     "excel_label": r["True_Positive"], "expected_noise": noise, "changed": "N",
                     "reason": f"True_Positive={r['True_Positive']}; consistent with '{r['Resolution_Notes']}'"})
    _write("alerts_expected.csv", rows)


def incidents() -> None:
    golden = {r["description"]: (r["category"], r["priority"]) for r in csv.DictReader(open(settings.GOLDEN_PATH))}
    labels = {**golden, **EXTRA_TRIAGE}
    inc = pd.read_excel(settings.FILES_DIR / "incident_log.xlsx", sheet_name="Incident Log")
    rows = []
    for r in inc.sort_values(["Reported_Date", "Incident_ID"]).to_dict("records"):
        cat, pri = labels[r["Short_Description"]]
        diffs = [f"category {r['Category']} -> {cat}" if cat != r["Category"] else "",
                 f"priority {r['Priority']} -> {pri}" if pri != r["Priority"] else ""]
        diffs = [d for d in diffs if d]
        src = "golden triage set" if r["Short_Description"] in golden else "reviewed for this check"
        rows.append({"id": r["Incident_ID"], "description": r["Short_Description"], "reported_by": r["Reported_By"],
                     "excel_category": r["Category"], "excel_priority": r["Priority"],
                     "expected_category": cat, "expected_priority": pri, "changed": "Y" if diffs else "N",
                     "reason": (("; ".join(diffs) + f" ({src}; the log labels each description two ways)") if diffs
                                else f"matches the {src}")})
    _write("incidents_expected.csv", rows)


def service_requests() -> None:
    db.configure()
    seed.run(reset=True, score_kb=False)
    roles = {e["employee_id"]: e["role"] for e in db.query("SELECT employee_id, role FROM employees")}
    ent = settings.load_yaml("entitlements")
    apps = list(settings.load_yaml("apps")["apps"])
    rng = random.Random(2025)

    def access(emp: str) -> tuple[str, str, str, str]:
        allowed = ent["roles"].get(roles[emp], {}).get("systems", [])
        roll = rng.random()
        if roll < ACCESS_MIX[0] and allowed:
            system = rng.choice(allowed)
            return system, "standard", "Y", f"standard access to {system} is in role ({roles[emp]})"
        if roll < ACCESS_MIX[0] + ACCESS_MIX[1]:
            system = rng.choice([a for a in apps if a not in allowed and a not in ent["privileged_systems"]])
            return system, "standard", "N", f"role {roles[emp]} is not entitled to {system}: needs a human"
        system = rng.choice(apps)
        return system, "admin", "N", f"admin access to {system} is privileged: needs a human approver"

    def row(rid, source, rtype, emp, dept, excel_label):
        system = level = None
        if rtype == "Password Reset":
            expected, why = "Y", "standard password reset for an active employee"
        elif rtype == "Access Provisioning":
            system, level, expected, why = access(emp)
        else:
            expected, why = "N", f"'{rtype}' is not a self-service catalogue item"
        changed = "Y" if excel_label and (excel_label == "High") != (expected == "Y") else "N"
        if changed == "Y":
            why += f" (Excel rates its automation potential {excel_label})"
        return {"id": rid, "source": source, "request_type": rtype, "requestor_id": emp, "department": dept,
                "channel": "portal" if rtype in SELF_SERVICE_TYPES else "ticket", "target_system": system or "",
                "access_level": level or "", "excel_label": excel_label or "", "expected_self_service": expected,
                "changed": changed, "reason": why}

    log = pd.read_excel(settings.FILES_DIR / "service_request_log.xlsx", sheet_name="Service Requests")
    vol = pd.read_excel(settings.FILES_DIR / "service_request_log.xlsx", sheet_name="Automation Opportunity")
    rows = [row(r["SR_ID"], "log", r["Request_Type"], r["Requestor_ID"], r["Department"], r["Automation_Potential"])
            for r in log.sort_values(["Submitted_Date", "SR_ID"]).to_dict("records")]
    in_log = log["Request_Type"].value_counts().to_dict()
    pool = sorted(e for e in roles if e.startswith("EMP7"))
    depts = {e["employee_id"]: e["department"] for e in db.query("SELECT employee_id, department FROM employees")}
    extra = []
    for v in vol.to_dict("records"):
        extra += [v["Request_Type"]] * max(0, int(v["Avg_Volume_Per_Month"]) - in_log.get(v["Request_Type"], 0))
    rng.shuffle(extra)
    for i, rtype in enumerate(extra, 1):
        emp = rng.choice(pool)
        rows.append(row(f"SRM{i:04d}", "monthly_volume", rtype, emp, depts[emp], None))
    _write("srs_expected.csv", rows)


def kb_articles() -> None:
    kb = pd.read_excel(settings.FILES_DIR / "kb_article_quality.xlsx")
    newest = kb.sort_values("Last_Updated").groupby("Title")["Article_ID"].last().to_dict()
    dup_count = kb["Title"].value_counts().to_dict()
    rows = []
    for r in kb.to_dict("records"):
        excel = str(r["Recommended_Action"]).lower()
        if dup_count[r["Title"]] > 1 and newest[r["Title"]] != r["Article_ID"]:
            expected, why = "archive", f"duplicate title; superseded by {newest[r['Title']]} (more recently updated)"
        else:
            expected, why = excel, f"Excel Recommended_Action={r['Recommended_Action']}"
        rows.append({"id": r["Article_ID"], "title": r["Title"], "excel_label": excel, "expected_action": expected,
                     "changed": "Y" if expected != excel else "N", "reason": why})
    _write("kb_expected.csv", rows)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    alerts()
    incidents()
    service_requests()
    kb_articles()
