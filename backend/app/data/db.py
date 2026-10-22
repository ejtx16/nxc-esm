"""SQLite shared data layer (DL-1..DL-4). One connection guarded by a re-entrant lock."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from app import settings

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
_path: Path | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
  alert_id TEXT PRIMARY KEY, alert_name TEXT, source_system TEXT, severity TEXT, alert_ts TEXT,
  acknowledged TEXT, ack_time_mins REAL, true_positive TEXT, incident_created TEXT, incident_id TEXT,
  auto_resolved TEXT, resolution_notes TEXT,
  origin TEXT DEFAULT 'seed', run_id TEXT, cluster_id TEXT, classification TEXT, confidence REAL,
  status TEXT DEFAULT 'new', truth_fp INTEGER, self_recovered INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_alerts_cluster ON alerts(cluster_id);
CREATE INDEX IF NOT EXISTS ix_alerts_run ON alerts(run_id);

CREATE TABLE IF NOT EXISTS clusters (
  cluster_id TEXT PRIMARY KEY, run_id TEXT, primary_system TEXT, signature TEXT, systems TEXT,
  first_alert_ts TEXT, last_alert_ts TEXT, alert_count INTEGER, max_severity TEXT, status TEXT,
  classification TEXT, confidence REAL, rationale TEXT, incident_id TEXT, ack_ts TEXT,
  ack_latency_s REAL, ack_real_ms REAL, truth_tp INTEGER, created_real REAL, mode TEXT
);
CREATE INDEX IF NOT EXISTS ix_clusters_run ON clusters(run_id);

CREATE TABLE IF NOT EXISTS incidents (
  incident_id TEXT PRIMARY KEY, category TEXT, priority TEXT, short_description TEXT, reported_by TEXT,
  reported_date TEXT, assigned_team TEXT, assigned_agent TEXT, status TEXT, resolution_time_hrs REAL,
  sla_breach TEXT, resolution_notes TEXT, origin TEXT DEFAULT 'seed', run_id TEXT, source_cluster_id TEXT,
  app_id TEXT, sla_hours REAL, opened_ts TEXT, breach_risk REAL DEFAULT 0, escalated INTEGER DEFAULT 0,
  escalated_ts TEXT, auto_resolvable INTEGER DEFAULT 0, confidence REAL, rationale TEXT,
  similar_ids TEXT, kb_article_ids TEXT, triaged_ts TEXT, triage_ms REAL, triage_mode TEXT,
  held INTEGER DEFAULT 0, runbook_id TEXT, provisional_priority TEXT
);

CREATE TABLE IF NOT EXISTS service_requests (
  sr_id TEXT PRIMARY KEY, request_type TEXT, requestor_id TEXT, department TEXT, submitted_ts TEXT,
  priority TEXT, status TEXT, fulfillment_time_hrs REAL, sla_target_hrs REAL, sla_met TEXT,
  fulfilled_by TEXT, manual_steps INTEGER, automation_potential TEXT,
  origin TEXT DEFAULT 'seed', run_id TEXT, channel TEXT DEFAULT 'ticket', target_system TEXT,
  access_level TEXT, text TEXT, trace TEXT, outcome TEXT, completed_ts TEXT
);
CREATE INDEX IF NOT EXISTS ix_sr_status ON service_requests(status);
CREATE INDEX IF NOT EXISTS ix_sr_run ON service_requests(run_id);

CREATE TABLE IF NOT EXISTS kb_articles (
  article_id TEXT PRIMARY KEY, title TEXT, category TEXT, created_date TEXT, last_updated TEXT, author TEXT,
  accuracy_score_pct REAL, completeness_score_pct REAL, usage_count_90days INTEGER,
  resolved_with_kb_count INTEGER, escalation_despite_kb INTEGER, outdated_flag TEXT,
  recommended_action_seed TEXT, body TEXT, systems TEXT,
  score REAL, recommendation TEXT, issues TEXT, rationale TEXT, scored_ts TEXT, score_parts TEXT,
  updated_by_agent INTEGER DEFAULT 0, assessment_mode TEXT, superseded_by TEXT
);

CREATE TABLE IF NOT EXISTS kb_drafts (
  draft_id INTEGER PRIMARY KEY AUTOINCREMENT, article_id TEXT, new_body TEXT, change_summary TEXT,
  sources TEXT, status TEXT DEFAULT 'pending', created_ts TEXT, decided_ts TEXT, mode TEXT, approval_id INTEGER
);

CREATE TABLE IF NOT EXISTS sla_summary (month TEXT, total_incidents INTEGER, p1_count INTEGER, p2_count INTEGER,
  p3_count INTEGER, p4_count INTEGER, sla_met INTEGER, sla_breached INTEGER, sla_compliance_pct REAL,
  avg_resolution_hrs REAL, mttr_hrs REAL);
CREATE TABLE IF NOT EXISTS alert_noise (source_system TEXT PRIMARY KEY, total_alerts_6mo INTEGER,
  true_positive INTEGER, false_positive INTEGER, noise_ratio_pct REAL, avg_ack_time_mins REAL,
  auto_resolved_pct REAL, mtta_mins REAL);
CREATE TABLE IF NOT EXISTS automation_opportunity (request_type TEXT PRIMARY KEY, avg_volume_per_month INTEGER,
  avg_fulfillment_hrs REAL, manual_steps_avg INTEGER, automation_potential TEXT, est_fte_savings REAL,
  est_time_saved_hrs_per_month INTEGER);

CREATE TABLE IF NOT EXISTS apps (app_id TEXT PRIMARY KEY, name TEXT, tier INTEGER, owner_team TEXT,
  users_affected INTEGER, depends_on TEXT, description TEXT);
CREATE TABLE IF NOT EXISTS employees (employee_id TEXT PRIMARY KEY, name TEXT, role TEXT, department TEXT,
  status TEXT DEFAULT 'active', email TEXT);
CREATE TABLE IF NOT EXISTS iam_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, employee_id TEXT, action TEXT,
  system TEXT, level TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS action_outcomes (id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT, entity_ref TEXT,
  ts TEXT, outcome TEXT);

CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, scenario TEXT, speed REAL, status TEXT,
  started_real REAL, started_sim TEXT, duration_sim_s REAL, total_events INTEGER, done_events INTEGER);

CREATE TABLE IF NOT EXISTS events (event_id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT, entity_ref TEXT,
  payload TEXT, ts_sim TEXT, ts_real REAL, status TEXT DEFAULT 'new', run_id TEXT, error TEXT, trace TEXT);
CREATE INDEX IF NOT EXISTS ix_events_status ON events(status, event_id);

-- Append-only audit log (DL-3). Agent, guardrail, execution and human rows are chained by parent_id.
CREATE TABLE IF NOT EXISTS agent_decisions (
  decision_id INTEGER PRIMARY KEY AUTOINCREMENT, parent_id INTEGER, kind TEXT, run_id TEXT, event_id INTEGER,
  agent TEXT, entity_ref TEXT, input_summary TEXT, reasoning TEXT, confidence REAL, proposed_action TEXT,
  guardrail_verdict TEXT, guardrail_reason TEXT, human_decision TEXT, mode TEXT, latency_ms REAL,
  timestamp TEXT, ts_real REAL, trace_id TEXT, span_id TEXT, iteration INTEGER
);
CREATE INDEX IF NOT EXISTS ix_dec_entity ON agent_decisions(entity_ref);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON agent_decisions
  BEGIN SELECT RAISE(ABORT, 'agent_decisions is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON agent_decisions
  BEGIN SELECT RAISE(ABORT, 'agent_decisions is append-only'); END;

CREATE TABLE IF NOT EXISTS approvals (
  approval_id INTEGER PRIMARY KEY AUTOINCREMENT, decision_id INTEGER, proposal_id TEXT, action TEXT, agent TEXT,
  entity_ref TEXT, summary TEXT, proposal TEXT, verdict TEXT, failed_rules TEXT, reasons TEXT, context TEXT,
  status TEXT, reason_code TEXT, created_ts TEXT, decided_ts TEXT, decided_by TEXT, note TEXT, result TEXT,
  run_id TEXT
);
CREATE INDEX IF NOT EXISTS ix_appr_status ON approvals(status);

CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS llm_calls (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, agent TEXT, schema TEXT,
  effort TEXT, input_tokens INTEGER, output_tokens INTEGER, cache_read INTEGER, cache_write INTEGER,
  latency_ms REAL, stop_reason TEXT, error TEXT, trace_id TEXT, span_id TEXT, run_id TEXT, event_id INTEGER,
  entity_ref TEXT, model TEXT, attempt INTEGER, cost_usd REAL, thinking_summary TEXT);

-- Audit trail spans (one trace per graph invocation; ADR-0004). Append-only like agent_decisions.
CREATE TABLE IF NOT EXISTS audit_spans (
  span_id TEXT PRIMARY KEY, trace_id TEXT, parent_span_id TEXT, run_id TEXT, event_id INTEGER, entity_ref TEXT,
  agent TEXT, span_type TEXT, name TEXT, iteration INTEGER, attempt INTEGER, status TEXT, error TEXT,
  decision_id INTEGER, started_sim TEXT, started_real REAL, latency_ms REAL, input_json TEXT, output_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_spans_trace ON audit_spans(trace_id);
CREATE INDEX IF NOT EXISTS ix_spans_run ON audit_spans(run_id);
CREATE TRIGGER IF NOT EXISTS spans_no_update BEFORE UPDATE ON audit_spans
  BEGIN SELECT RAISE(ABORT, 'audit_spans is append-only'); END;
CREATE TRIGGER IF NOT EXISTS spans_no_delete BEFORE DELETE ON audit_spans
  BEGIN SELECT RAISE(ABORT, 'audit_spans is append-only'); END;

CREATE TABLE IF NOT EXISTS accuracy_results (
  id INTEGER PRIMARY KEY AUTOINCREMENT, trace_id TEXT, decision_id INTEGER, run_id TEXT, agent TEXT, scope TEXT,
  metric TEXT, predicted TEXT, expected TEXT, correct INTEGER, score REAL, confidence REAL, source TEXT,
  mode TEXT, ts TEXT
);
CREATE INDEX IF NOT EXISTS ix_acc_agent ON accuracy_results(agent);
CREATE TRIGGER IF NOT EXISTS acc_no_update BEFORE UPDATE ON accuracy_results
  BEGIN SELECT RAISE(ABORT, 'accuracy_results is append-only'); END;
CREATE TRIGGER IF NOT EXISTS acc_no_delete BEFORE DELETE ON accuracy_results
  BEGIN SELECT RAISE(ABORT, 'accuracy_results is append-only'); END;
CREATE TABLE IF NOT EXISTS correlation_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, run_id TEXT, decision_id INTEGER, alert_id TEXT, cluster_id TEXT,
  grouping TEXT, signature TEXT, source_system TEXT, severity TEXT, cluster_size INTEGER, cluster_systems TEXT,
  classification TEXT, confidence REAL, grouping_confidence REAL, mode TEXT, rationale TEXT, explanation TEXT
);
CREATE INDEX IF NOT EXISTS ix_corr_cluster ON correlation_log(cluster_id);
CREATE INDEX IF NOT EXISTS ix_corr_run ON correlation_log(run_id);
CREATE TABLE IF NOT EXISTS kpi_results (id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS kpi_excel_results (id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT, payload TEXT);
"""

TABLES_TO_RESET = [
    "alerts", "clusters", "incidents", "service_requests", "kb_articles", "kb_drafts", "sla_summary",
    "alert_noise", "automation_opportunity", "apps", "employees", "iam_log", "action_outcomes", "runs",
    "events", "agent_decisions", "approvals", "settings", "llm_calls", "audit_spans", "accuracy_results",
    "correlation_log",
]
# kpi_results / kpi_excel_results are kept: they come from replays in a separate database, not from the live data.

# Columns added after the first release; `migrate()` adds them to databases created before (ADR-0004).
ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "correlation_log": {"explanation": "TEXT"},
    "agent_decisions": {"trace_id": "TEXT", "span_id": "TEXT", "iteration": "INTEGER"},
    "llm_calls": {"trace_id": "TEXT", "span_id": "TEXT", "run_id": "TEXT", "event_id": "INTEGER",
                  "entity_ref": "TEXT", "model": "TEXT", "attempt": "INTEGER", "cost_usd": "REAL",
                  "thinking_summary": "TEXT"},
}


def configure(path: str | Path | None = None) -> Path:
    """(Re)point the data layer at a database file; creates schema if missing."""
    global _conn, _path
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None
        _path = Path(path) if path else (_path or settings.db_path())     # no path: keep the current database
        _path.parent.mkdir(parents=True, exist_ok=True)
        _connect()
    return _path


def _connect() -> sqlite3.Connection:
    global _conn, _path
    if _conn is None:
        if _path is None:
            _path = settings.db_path()
            _path.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(_path), check_same_thread=False, isolation_level=None)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=OFF")
        _conn.executescript(SCHEMA)
        _migrate(_conn)
    return _conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns that older databases lack (ALTER is allowed on the append-only tables)."""
    for table, cols in ADDED_COLUMNS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, typ in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")


def db_file() -> Path:
    _connect()
    assert _path is not None
    return _path


def reset_schema() -> None:
    """Drop all tables (including the append-only audit table) and recreate them."""
    with _lock:
        conn = _connect()
        for t in TABLES_TO_RESET:
            conn.execute(f"DROP TABLE IF EXISTS {t}")
        conn.executescript(SCHEMA)


AUDIT_TABLES = ["agent_decisions", "audit_spans", "accuracy_results", "llm_calls", "correlation_log"]


def clear_audit() -> None:
    """Empty the audit trail. DROP bypasses the append-only triggers, which SCHEMA then recreates."""
    with _lock:
        conn = _connect()
        for t in AUDIT_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {t}")
        conn.executescript(SCHEMA)


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    """Group writes into one transaction (also holds the lock for the duration)."""
    with _lock:
        conn = _connect()
        if conn.in_transaction:
            yield conn
            return
        conn.execute("BEGIN")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    with _lock:
        cur = _connect().execute(sql, tuple(params))
        return cur.lastrowid or 0


def executemany(sql: str, rows: Iterable[Iterable[Any]]) -> None:
    with _lock:
        _connect().executemany(sql, [tuple(r) for r in rows])


def query(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    with _lock:
        return [dict(r) for r in _connect().execute(sql, tuple(params)).fetchall()]


def one(sql: str, params: Iterable[Any] = ()) -> dict | None:
    with _lock:
        r = _connect().execute(sql, tuple(params)).fetchone()
        return dict(r) if r else None


def scalar(sql: str, params: Iterable[Any] = (), default: Any = None) -> Any:
    with _lock:
        r = _connect().execute(sql, tuple(params)).fetchone()
        return r[0] if r and r[0] is not None else default


def insert(table: str, row: dict) -> int:
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    return execute(f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({marks})", row.values())


def update(table: str, key_col: str, key: Any, fields: dict) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    execute(f"UPDATE {table} SET {sets} WHERE {key_col}=?", [*fields.values(), key])


def dumps(obj: Any) -> str:
    return json.dumps(obj, default=str, ensure_ascii=False)


def loads(text: str | None, default: Any = None) -> Any:
    if text is None or text == "":
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


# --- settings (operator state: kill switch, LLM mode, surge) ---------------------------------
DEFAULT_SETTINGS: dict[str, Any] = {
    "kill_switch": {"global": False, "agents": {}, "apps": {}},
    "llm_mode": None,            # None -> follow LLM_MODE env
    "surge_mode": False,
    "triage_hold": False,
    "suppressions_enabled": True,
}


def get_setting(key: str, default: Any = None) -> Any:
    row = one("SELECT value FROM settings WHERE key=?", (key,))
    if row is None:
        return DEFAULT_SETTINGS.get(key, default)
    return loads(row["value"], default)


def set_setting(key: str, value: Any) -> None:
    execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, dumps(value)))
