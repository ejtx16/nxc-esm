"""Audit trail API (AU-1..AU-8): traces, spans and per-agent metrics for the six audit dimensions."""
from __future__ import annotations

import csv
import io
import json
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from app.api.util import page, shape, shape_all
from app.data import db

router = APIRouter(prefix="/audit")

EXPORT_TABLES = {"spans": ("audit_spans", "started_real"), "decisions": ("agent_decisions", "decision_id"),
                 "llm_calls": ("llm_calls", "id"), "accuracy": ("accuracy_results", "id"),
                 "correlation": ("correlation_log", "id")}
CONF_BUCKETS = [(0.0, 0.5), (0.5, 0.7), (0.7, 0.85), (0.85, 1.01)]


def _where(run_id: str | None, agent: str | None = None, *, agent_col: str = "agent") -> tuple[str, list]:
    clauses, args = [], []
    if run_id:
        clauses.append("run_id=?")
        args.append(run_id)
    if agent:
        clauses.append(f"{agent_col}=?")
        args.append(agent)
    return ("WHERE " + " AND ".join(clauses)) if clauses else "", args


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    vals = sorted(values)
    idx = min(len(vals) - 1, max(0, round(q * (len(vals) - 1))))
    return round(vals[idx], 2)


@router.get("/traces")
def traces(run_id: str | None = None, agent: str | None = None, entity: str | None = None,
           status: str | None = None, limit: int | None = None) -> list[dict]:
    """One row per trace (event or operator action), newest first."""
    clauses, args = [], []
    for col, val in (("run_id", run_id), ("entity_ref", entity)):
        if val:
            clauses.append(f"{col}=?")
            args.append(val)
    if agent:
        clauses.append("trace_id IN (SELECT trace_id FROM audit_spans WHERE agent=?)")
        args.append(agent)
    if status == "error":
        clauses.append("trace_id IN (SELECT trace_id FROM audit_spans WHERE status!='ok')")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = db.query(
        f"""SELECT trace_id, MIN(run_id) run_id, MIN(event_id) event_id, MIN(started_sim) started_sim,
            MIN(started_real) t0, MAX(started_real + latency_ms / 1000.0) t1,
            MAX(CASE WHEN span_type='node' THEN iteration END) steps,
            SUM(CASE WHEN span_type='tool' THEN 1 ELSE 0 END) tool_calls,
            SUM(CASE WHEN span_type='llm' THEN 1 ELSE 0 END) llm_calls,
            SUM(CASE WHEN span_type='llm' AND attempt > 1 THEN 1 ELSE 0 END) retries,
            SUM(CASE WHEN status!='ok' THEN 1 ELSE 0 END) issues,
            GROUP_CONCAT(DISTINCT agent) agents
            FROM audit_spans {where} GROUP BY trace_id ORDER BY t0 DESC LIMIT ?""", (*args, page(limit)))
    if not rows:
        return []
    ids = [r["trace_id"] for r in rows]
    marks = ",".join("?" * len(ids))
    events = {e["event_id"]: e for e in db.query(
        f"SELECT event_id, type, entity_ref FROM events WHERE event_id IN ({','.join('?' * len(rows))})",
        [r["event_id"] for r in rows])}
    tokens = {t["trace_id"]: t for t in db.query(
        f"""SELECT trace_id, SUM(input_tokens) input_tokens, SUM(output_tokens) output_tokens,
            SUM(cache_read) cache_read, ROUND(SUM(cost_usd), 6) cost_usd FROM llm_calls
            WHERE trace_id IN ({marks}) GROUP BY trace_id""", ids)}
    acc = {a["trace_id"]: a for a in db.query(
        f"""SELECT trace_id, COUNT(*) n, SUM(correct) correct FROM accuracy_results
            WHERE trace_id IN ({marks}) AND correct IS NOT NULL GROUP BY trace_id""", ids)}
    entity_by_trace = {r["trace_id"]: r["entity_ref"] for r in db.query(
        f"SELECT trace_id, entity_ref FROM audit_spans WHERE trace_id IN ({marks}) AND parent_span_id IS NULL "
        f"ORDER BY started_real", ids)}
    out = []
    for r in rows:
        ev = events.get(r["event_id"]) or {}
        t = tokens.get(r["trace_id"], {})
        a = acc.get(r["trace_id"])
        out.append({
            "trace_id": r["trace_id"], "run_id": r["run_id"], "event_id": r["event_id"],
            "event_type": ev.get("type"), "entity_ref": ev.get("entity_ref") or entity_by_trace.get(r["trace_id"]),
            "started_sim": r["started_sim"], "agents": sorted(set((r["agents"] or "").split(",")) - {""}),
            "steps": r["steps"] or 0, "tool_calls": r["tool_calls"], "llm_calls": r["llm_calls"],
            "retries": r["retries"], "issues": r["issues"],
            "latency_ms": round((r["t1"] - r["t0"]) * 1000, 2) if r["t1"] is not None else None,
            "input_tokens": t.get("input_tokens") or 0, "output_tokens": t.get("output_tokens") or 0,
            "cache_read": t.get("cache_read") or 0, "cost_usd": t.get("cost_usd") or 0.0,
            "accuracy": {"n": a["n"], "correct": a["correct"]} if a else None})
    return out


GROUPING_RULES = [
    "Repeat: if the same alert (same system, same name) fires again shortly after the last one, it joins that group "
    "as a duplicate.",
    "Related: if a serious problem (High or Critical) is already being handled and a new alert comes from the same "
    "system or one it depends on, it joins that group, because they probably share a cause.",
    "New: if neither applies, the alert starts a new group.",
    "Verdict: each new group is judged real or noise from history (how noisy the system is, how often this alert was "
    "a false alarm, whether it fixed itself). Critical alerts are never treated as noise.",
]


@router.get("/correlation")
def correlation(run_id: str | None = None, cluster_id: str | None = None, grouping: str | None = None,
                limit: int | None = None) -> dict:
    """How the alert correlation agent grouped each alert and how confident it was (newest first)."""
    clauses, args = [], []
    for col, val in (("run_id", run_id), ("cluster_id", cluster_id), ("grouping", grouping)):
        if val:
            clauses.append(f"{col}=?")
            args.append(val)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = db.query(f"SELECT * FROM correlation_log {where} ORDER BY id DESC LIMIT ?", (*args, page(limit)))
    for r in rows:
        r["cluster_systems"] = db.loads(r["cluster_systems"], [])
    summary = db.query(f"""SELECT grouping, COUNT(*) alerts, ROUND(AVG(confidence), 3) avg_confidence,
        ROUND(MIN(confidence), 3) min_confidence FROM correlation_log {where} GROUP BY grouping""", args)
    return {"how_it_works": GROUPING_RULES, "summary": summary, "rows": rows}


@router.get("/traces/{trace_id}")
def trace_detail(trace_id: str) -> dict:
    spans = shape_all(db.query("SELECT * FROM audit_spans WHERE trace_id=? ORDER BY started_real", (trace_id,)),
                      ("input_json", "output_json"))
    if not spans:
        raise HTTPException(404, f"trace {trace_id} not found")
    t0 = spans[0]["started_real"]
    for s in spans:
        s["offset_ms"] = round((s.pop("started_real") - t0) * 1000, 3)
        s["input"], s["output"] = s.pop("input_json"), s.pop("output_json")
    event = shape(db.one("SELECT event_id, type, entity_ref, payload, ts_sim, status, error FROM events "
                         "WHERE event_id=?", (spans[0]["event_id"],)), ("payload",)) if spans[0]["event_id"] else None
    return {
        "trace_id": trace_id, "event": event, "spans": spans,
        "decisions": shape_all(db.query("SELECT * FROM agent_decisions WHERE trace_id=? ORDER BY decision_id",
                                        (trace_id,)), ("proposed_action",)),
        "llm_calls": db.query("SELECT * FROM llm_calls WHERE trace_id=? ORDER BY id", (trace_id,)),
        "accuracy": db.query("SELECT * FROM accuracy_results WHERE trace_id=? ORDER BY id", (trace_id,))}


@router.get("/metrics")
def metrics(run_id: str | None = None, agent: str | None = None) -> dict:
    """Per-agent rollup across the six dimensions, plus overall totals and the latest batch evaluation."""
    w, a = _where(run_id, agent)
    spans = db.query(f"SELECT trace_id, agent, span_type, name, iteration, attempt, status, latency_ms "
                     f"FROM audit_spans {w}", a)
    llm = db.query(f"SELECT agent, input_tokens, output_tokens, cache_read, cache_write, cost_usd, attempt, error "
                   f"FROM llm_calls {w}", a)
    acc = db.query(f"SELECT agent, source, metric, correct, score, confidence FROM accuracy_results {w}", a)
    dec = db.query(f"SELECT agent, kind, guardrail_verdict, human_decision FROM agent_decisions {w}", a)

    agents = sorted({r["agent"] for r in spans + llm + acc + dec if r["agent"]})
    per_agent = {name: _agent_block(name, spans, llm, acc, dec) for name in agents}
    return {"filters": {"run_id": run_id, "agent": agent}, "agents": per_agent,
            "totals": _agent_block(None, spans, llm, acc, dec), "batch_evals": _batch_evals()}


def _agent_block(name: str | None, spans: list, llm: list, acc: list, dec: list) -> dict:
    mine = (lambda rows: rows) if name is None else (lambda rows: [r for r in rows if r["agent"] == name])
    sp, lc, ac, dc = mine(spans), mine(llm), mine(acc), mine(dec)
    nodes = [s for s in sp if s["span_type"] == "node"]
    steps_by_trace: dict[str, int] = {}
    for s in nodes:
        steps_by_trace[s["trace_id"]] = max(steps_by_trace.get(s["trace_id"], 0), s["iteration"] or 0)
    llm_spans = [s for s in sp if s["span_type"] == "llm"]
    tools: dict[str, dict] = {}
    for s in (s for s in sp if s["span_type"] == "tool"):
        t = tools.setdefault(s["name"], {"calls": 0, "errors": 0, "lat": []})
        t["calls"] += 1
        t["errors"] += s["status"] != "ok"
        t["lat"].append(s["latency_ms"] or 0)
    latency: dict[str, dict] = {}
    for s in sp:
        latency.setdefault(s["span_type"], []).append(s["latency_ms"] or 0)
    graded = [r for r in ac if r["correct"] is not None]
    by_source: dict[str, dict] = {}
    for r in graded:
        b = by_source.setdefault(r["source"], {"n": 0, "correct": 0})
        b["n"] += 1
        b["correct"] += r["correct"]
    calibration = []
    for lo, hi in CONF_BUCKETS:
        rows = [r for r in graded if r["confidence"] is not None and lo <= r["confidence"] < hi]
        if rows:
            calibration.append({"bucket": f"{lo:.2f}-{min(hi, 1):.2f}", "n": len(rows),
                                "avg_confidence": round(sum(r["confidence"] for r in rows) / len(rows), 3),
                                "accuracy_pct": round(100 * sum(r["correct"] for r in rows) / len(rows), 1)})
    kinds: dict[str, int] = {}
    verdicts: dict[str, int] = {}
    for r in dc:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
        if r["kind"] == "guardrail" and r["guardrail_verdict"]:
            verdicts[r["guardrail_verdict"]] = verdicts.get(r["guardrail_verdict"], 0) + 1
    human = [r for r in dc if r["kind"] == "human"]
    overrides = [r for r in graded if r["source"] == "human_override"]
    return {
        "iteration": {"traces": len(steps_by_trace), "node_runs": len(nodes),
                      "avg_steps": round(sum(steps_by_trace.values()) / len(steps_by_trace), 2) if steps_by_trace else 0,
                      "max_steps": max(steps_by_trace.values(), default=0),
                      "llm_attempts": len(llm_spans), "retries": sum(1 for s in llm_spans if (s["attempt"] or 1) > 1),
                      "errors": sum(1 for s in sp if s["status"] != "ok")},
        "tools": {"calls": sum(t["calls"] for t in tools.values()),
                  "errors": sum(t["errors"] for t in tools.values()),
                  "by_tool": sorted(({"tool": k, "calls": v["calls"], "errors": v["errors"],
                                      "p50_ms": _pct(v["lat"], 0.5), "p95_ms": _pct(v["lat"], 0.95)}
                                     for k, v in tools.items()), key=lambda x: -x["calls"])},
        "tokens": {"calls": len(lc), "input": sum(r["input_tokens"] or 0 for r in lc),
                   "output": sum(r["output_tokens"] or 0 for r in lc),
                   "cache_read": sum(r["cache_read"] or 0 for r in lc),
                   "cache_write": sum(r["cache_write"] or 0 for r in lc),
                   "cost_usd": round(sum(r["cost_usd"] or 0 for r in lc), 6),
                   "errors": sum(1 for r in lc if r["error"])},
        "accuracy": {"graded": len(graded),
                     "accuracy_pct": round(100 * sum(r["correct"] for r in graded) / len(graded), 1) if graded else None,
                     "by_source": {k: {**v, "accuracy_pct": round(100 * v["correct"] / v["n"], 1)}
                                   for k, v in by_source.items()},
                     "calibration": calibration,
                     "kb_scores": [r["score"] for r in ac if r["source"] == "kb_rescore"][-10:]},
        "activity": {"decisions": len(dc), "by_kind": kinds, "guardrail_verdicts": verdicts,
                     "human_decisions": len(human),
                     "override_rate_pct": round(100 * sum(1 for r in overrides if not r["correct"]) / len(overrides), 1)
                     if overrides else None},
        "latency": {k: {"n": len(v), "p50_ms": _pct(v, 0.5), "p95_ms": _pct(v, 0.95), "max_ms": round(max(v), 2)}
                    for k, v in latency.items()},
    }


def _batch_evals() -> dict | None:
    row = db.one("SELECT payload, created FROM kpi_results ORDER BY id DESC LIMIT 1")
    payload = db.loads(row["payload"], {}) if row else {}
    if not payload.get("evals"):
        from app import settings
        path = settings.DATA_DIR / "kpi_results.json"
        if path.exists():
            payload = json.loads(path.read_text())
    return payload.get("evals")


@router.get("/export")
def export(table: str = "spans", run_id: str | None = None, format: str = "csv") -> Response:
    if table not in EXPORT_TABLES:
        raise HTTPException(400, f"table must be one of {sorted(EXPORT_TABLES)}")
    if format not in ("csv", "jsonl"):
        raise HTTPException(400, "format must be csv or jsonl")
    name, order = EXPORT_TABLES[table]
    w, a = _where(run_id)
    rows: list[dict[str, Any]] = db.query(f"SELECT * FROM {name} {w} ORDER BY {order}", a)
    fname = f"esm-audit-{table}{'-' + run_id if run_id else ''}.{format}"
    if format == "jsonl":
        body = "\n".join(json.dumps(r, default=str) for r in rows)
        media = "application/x-ndjson"
    else:
        buf = io.StringIO()
        if rows:
            writer = csv.DictWriter(buf, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        body, media = buf.getvalue(), "text/csv"
    return Response(body, media_type=media, headers={"Content-Disposition": f'attachment; filename="{fname}"'})
