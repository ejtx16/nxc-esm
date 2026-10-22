"""Excel accuracy check: replays the curated Excel rows (scenario `excel_replay`) in an ISOLATED database and reports,
for the five leading indicators, the baseline recomputed from the spreadsheets, the value after the agents ran, and
how often the agent decisions agree with the curated answer key in data/curated (and with the raw Excel labels)."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from app import settings
from app.kpi import baseline as base
from app.kpi.simulate import _pct

_status: dict = {"running": False, "started": None, "error": None, "finished": None}
PRIORITIES = ["P1", "P2", "P3", "P4"]


# --- helpers (run inside the isolated process) -----------------------------------------------------------
def excel_baselines() -> dict:
    """The 'before' figures, recomputed from the spreadsheets (not quoted)."""
    import pandas as pd
    f = settings.FILES_DIR
    alerts = pd.read_excel(f / "monitoring_alerts.xlsx", sheet_name="Alert Log")
    srs = pd.read_excel(f / "service_request_log.xlsx", sheet_name="Service Requests")
    kb = pd.read_excel(f / "kb_article_quality.xlsx")
    return {
        "alert_noise": (_pct((alerts["True_Positive"] == "No").sum(), len(alerts)),
                        f"{(alerts['True_Positive'] == 'No').sum()} of {len(alerts)} logged alerts are false positives"),
        "ack_time": (round(float(alerts["Ack_Time_Mins"].mean()), 1),
                     f"mean Ack_Time_Mins of the {alerts['Ack_Time_Mins'].notna().sum()} acknowledged alerts"),
        "triage_time": (base.BY_KEY["triage_time"]["baseline"], "case-study figure (not in the spreadsheets)"),
        "self_service": (_pct((srs["Automation_Potential"] == "High").sum(), len(srs)),
                         f"{(srs['Automation_Potential'] == 'High').sum()} of {len(srs)} logged SRs rated High "
                         "automation potential (all were fulfilled by people)"),
        "kb_accuracy": (round(float(kb["Accuracy_Score_Pct"].mean()), 1),
                        f"mean Accuracy_Score_Pct of the {len(kb)} articles"),
    }


def _grade(pairs: list[tuple[str, object, object]], vs_excel: list[tuple[object, object]] | None = None) -> dict:
    """pairs: (row id, predicted, expected). Returns counts, % and up to 10 misses."""
    ok = [p == e for _, p, e in pairs]
    out = {"n": len(pairs), "correct": sum(ok), "pct": _pct(sum(ok), len(pairs)),
           "misses": [f"{i}: got {p}, expected {e}" for (i, p, e), good in zip(pairs, ok) if not good][:10]}
    if vs_excel is not None:
        out["vs_excel_pct"] = _pct(sum(p == e for p, e in vs_excel), len(vs_excel))
    return out


def _replayed(table: str, run_id: str, kind_rows: list[dict], where: str = "") -> list[tuple[dict, dict]]:
    """Pair curated rows with the records the replay created (same order: the player emits in event order)."""
    from app.data import db
    got = db.query(f"SELECT * FROM {table} WHERE run_id=? {where} ORDER BY rowid", (run_id,))
    if len(got) != len(kind_rows):
        raise RuntimeError(f"{table}: replay created {len(got)} rows for {len(kind_rows)} curated rows")
    return list(zip(kind_rows, got))


def _alert_accuracy(run_id: str, name: str = "alerts") -> tuple[dict, dict]:
    from app.data import db, scenarios
    rows = _replayed("alerts", run_id, scenarios.curated(name))
    status = {c["cluster_id"]: c for c in db.query("SELECT * FROM clusters WHERE run_id=?", (run_id,))}
    pairs, acks = [], []
    fp_caught = tp_suppressed = 0
    for cur, a in rows:
        cl = status.get(a["cluster_id"]) or {}
        noise = cl.get("status") == "suppressed"
        expected = cur["expected_noise"] == "Y"
        pairs.append((cur["id"], "noise" if noise else "real", "noise" if expected else "real"))
        fp_caught += noise and expected
        tp_suppressed += noise and not expected
        if not expected and cur["severity"] == "Critical":       # the agent auto-acknowledges Critical alerts only
            acks.append((cur["id"], "acknowledged" if cl.get("ack_ts") else "not acknowledged", "acknowledged"))
    noise_acc = _grade(pairs, [(p, "noise" if c["excel_label"] == "No" else "real")
                               for (_, p, _), (c, _) in zip(pairs, rows)])
    flagged = sum(1 for _, p, _ in pairs if p == "noise")
    actual = sum(1 for _, _, e in pairs if e == "noise")
    noise_acc.update(precision_pct=_pct(fp_caught, flagged), recall_pct=_pct(fp_caught, actual),
                     true_positives_wrongly_suppressed=tp_suppressed)
    ack_acc = _grade(acks)
    ack_acc["note"] = f"share of the {len(acks)} curated true-positive Critical alerts the agent acknowledged"
    return noise_acc, ack_acc


def _triage_accuracy(run_id: str) -> dict:
    from app.data import scenarios
    rows = _replayed("incidents", run_id, scenarios.curated("incidents"), "AND source_cluster_id IS NULL")
    cat = [(c["id"], i["category"], c["expected_category"]) for c, i in rows]
    out = _grade(cat, [(i["category"], c["excel_category"]) for c, i in rows])

    def within(p, e):
        return p in PRIORITIES and abs(PRIORITIES.index(p) - PRIORITIES.index(e)) <= 1
    out["priority_within_1_pct"] = _pct(sum(within(i["priority"], c["expected_priority"]) for c, i in rows), len(rows))
    out["priority_exact_pct"] = _pct(sum(i["priority"] == c["expected_priority"] for c, i in rows), len(rows))
    out["note"] = "category exact match; priority graded separately"
    return out


def _sr_accuracy(run_id: str) -> dict:
    from app.data import scenarios
    rows = _replayed("service_requests", run_id, scenarios.curated("srs"))

    def auto(r):
        return "self-served" if (r["fulfilled_by"] or "").startswith("agent:") else "human"
    pairs = [(c["id"], auto(s), "self-served" if c["expected_self_service"] == "Y" else "human") for c, s in rows]
    out = _grade(pairs, [(auto(s), "self-served" if c["excel_label"] == "High" else "human")
                         for c, s in rows if c["source"] == "log"])
    out["wrongly_self_served"] = sum(1 for _, p, e in pairs if p == "self-served" and e == "human")
    out["note"] = "vs_excel_pct grades only the 60 logged SRs against Automation_Potential=High"
    return out


def _kb_accuracy() -> dict:
    """Grades the KB agent's initial review (recommendation per article) against the curated action."""
    from app.data import db, scenarios
    recs = {a["article_id"]: a["recommendation"] for a in db.query("SELECT article_id, recommendation FROM kb_articles")}
    cur = scenarios.curated("kb")
    return _grade([(c["id"], recs.get(c["id"]), c["expected_action"]) for c in cur],
                  [(recs.get(c["id"]), c["excel_label"]) for c in cur])


def _simulated_noise() -> dict:
    """Second pass on the simulated alert stream. Its labels follow a pattern, so this shows whether the noise logic
    works when there is signal to find; the 80 logged alerts cannot show that. Synthetic, never a real-world figure."""
    from app.kpi import simulate
    from app.runtime import player
    simulate._fresh()
    rid = player.run_sync("alert_noise_sim")
    m = simulate._alert_metrics(rid)
    acc, _ = _alert_accuracy(rid, "alerts_simulated")
    return {"simulated_alerts": acc["n"], "simulated_accuracy_pct": acc["pct"],
            "simulated_precision_pct": acc["precision_pct"], "simulated_recall_pct": acc["recall_pct"],
            "simulated_noise_before_pct": _pct(m["raw_fp"], m["raw_alerts"]),
            "simulated_noise_after_pct": _pct(m["surfaced_fp"], m["surfaced"]),
            "simulated_real_alerts_wrongly_suppressed": acc["true_positives_wrongly_suppressed"],
            "simulated_misses": acc["misses"]}


def run_check() -> dict:
    from app.data import db, scenarios
    from app.kpi import simulate
    from app.llm import client as llm
    from app.runtime import player
    db.configure()
    t0 = time.time()
    llm.set_client(None)

    simulate._fresh()
    rid = player.run_sync("excel_replay")
    alerts = simulate._alert_metrics(rid)
    tri = simulate._triage_ms()
    sr = simulate._sr_metrics()
    noise_acc, ack_acc = _alert_accuracy(rid)
    triage_acc = _triage_accuracy(rid)
    sr_acc = _sr_accuracy(rid)

    simulate._fresh()                                  # KB is graded and modelled on an untouched corpus
    kb_acc = _kb_accuracy()
    kb = simulate._kb_model()
    noise_acc.update(_simulated_noise())              # last: it resets the database again

    before = excel_baselines()
    ack_ms = sum(alerts["acks_real_ms"]) / len(alerts["acks_real_ms"]) if alerts["acks_real_ms"] else 0.0
    tri_ms = sum(tri) / len(tri) if tri else 0.0

    def entry(key, after, kind, method, accuracy, detail, display=None):
        b = base.BY_KEY[key]
        met = after <= b["target"] if b["better"] == "lower" else after >= b["target"]
        value, source = before[key]
        return {"key": key, "label": b["label"], "unit": b["unit"], "better": b["better"], "baseline": value,
                "baseline_source": source, "after": round(after, 3) if after < 1 else round(after, 1),
                "after_display": display, "target": b["target"], "target_text": b["target_text"],
                "target_met": bool(met), "kind": kind, "method": method, "accuracy": accuracy, "detail": detail}

    kpis = [
        entry("alert_noise", _pct(alerts["surfaced_fp"], alerts["surfaced"]), "measured",
              "False-positive clusters still surfaced / all surfaced clusters after replaying the 80 logged alerts",
              noise_acc, {k: alerts[k] for k in ("raw_alerts", "raw_fp", "clusters", "surfaced", "surfaced_fp",
                                                 "suppressed", "tp_wrongly_suppressed")}),
        entry("ack_time", ack_ms / 60000, "measured",
              "Real processing latency from alert received to acknowledgement for auto-acknowledged clusters",
              ack_acc, {"clusters_acked": len(alerts["acks_real_ms"]), "mean_ms": round(ack_ms, 1)},
              display=f"{ack_ms / 1000:.2f} s"),
        entry("triage_time", tri_ms / 60000, "measured",
              "Real latency from incident created to triage decision for the 50 logged incidents (rules mode)",
              triage_acc, {"incidents_triaged": len(tri), "mean_ms": round(tri_ms, 1)},
              display=f"{tri_ms / 1000:.2f} s"),
        entry("self_service", sr["self_service_pct"], "measured",
              "SRs fulfilled by the agent / all SRs in one month at the logged Automation Opportunity volumes",
              sr_acc, {"total": sr["total"], "auto": sr["auto"], "by_status": sr["by_status"]}),
        entry("kb_accuracy", kb["mean_after_active"], "modelled",
              "Mean recomputed score of the maintained corpus after every drafted update is approved (approval assumed)",
              kb_acc, {k: kb[k] for k in ("mean_before_all", "mean_after_active", "updated", "archived", "below_after")}),
    ]
    files = {n: scenarios.curated(n) for n in ("alerts", "incidents", "srs", "kb")}
    return {"generated": time.strftime("%Y-%m-%d %H:%M:%S"), "llm_mode": "rules", "run_id": rid,
            "dataset": {n: {"rows": len(r), "curated_overrides": sum(x["changed"] == "Y" for x in r)}
                        for n, r in files.items()},
            "kpis": kpis, "duration_s": round(time.time() - t0, 1)}


# --- API-process side ------------------------------------------------------------------------------------
def _run_subprocess() -> None:
    from app.data import db
    out = settings.DATA_DIR / "kpi_excel_results.json"
    env = {**os.environ, "NEXA_DB_PATH": str(settings.DATA_DIR / "kpi_excel.db"), "LLM_MODE": "rules",
           "NEXA_SIM_START": settings.CASE_START.isoformat()}   # graded against the case calendar, not today
    _status.update(running=True, started=time.time(), error=None)
    try:
        proc = subprocess.run([sys.executable, "-m", "app.kpi.excel_check", "--out", str(out)], cwd=settings.BASE_DIR,
                              env=env, capture_output=True, text=True, timeout=900)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr[-1500:] or proc.stdout[-500:])
        payload = json.loads(out.read_text())
        db.execute("INSERT INTO kpi_excel_results (created, payload) VALUES (?,?)", (payload["generated"], json.dumps(payload)))
        _status.update(finished=time.time())
    except Exception as exc:  # noqa: BLE001
        _status.update(error=f"{type(exc).__name__}: {exc}")
    finally:
        _status["running"] = False


def start_background() -> dict:
    if not _status["running"]:
        _status["running"] = True
        threading.Thread(target=_run_subprocess, daemon=True, name="kpi-excel-check").start()
    return {"started": True, **_status}


def latest() -> dict:
    from app.data import db
    row = db.one("SELECT payload FROM kpi_excel_results ORDER BY id DESC LIMIT 1")
    return {"results": json.loads(row["payload"]) if row else None, "status": dict(_status)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    res = run_check()
    Path(args.out).write_text(json.dumps(res, indent=2))
    print(f"Excel accuracy check finished in {res['duration_s']}s")


if __name__ == "__main__":
    main()
