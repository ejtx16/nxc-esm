export type Priority = "P1" | "P2" | "P3" | "P4";

export interface Sla { window_h: number; elapsed_h: number | null; remaining_h: number | null; risk: number | null; breached: boolean }

export interface Incident {
  incident_id: string; priority: Priority; category: string | null; short_description: string;
  assigned_team: string | null; status: string; app_id: string | null; tier: number | null;
  confidence: number | null; rationale: string | null; escalated: number; held: number;
  auto_resolvable: number; runbook_id: string | null; source_cluster_id: string | null;
  kb_article_ids: string[]; similar_ids: string[]; sla: Sla; pending_approvals: number;
  triage_ms: number | null; triage_mode: string | null; resolution_notes: string | null; opened_ts: string | null;
}

export interface Decision {
  decision_id: number; parent_id: number | null;
  kind: "agent" | "guardrail" | "execution" | "human" | "queued" | "orchestrator_failure";
  agent: string; entity_ref: string; input_summary: string | null; reasoning: string | null;
  confidence: number | null; guardrail_verdict: string | null; guardrail_reason: string | null;
  human_decision: string | null; mode: string | null; latency_ms: number | null; timestamp: string;
  proposed_action: unknown; trace_id?: string | null; span_id?: string | null; iteration?: number | null;
}

export interface Approval {
  approval_id: number; action: string; agent: string; entity_ref: string; summary: string;
  verdict: "RequireApproval" | "Block"; failed_rules: string[]; reasons: string[];
  context: Record<string, unknown>; status: string; reason_code: string | null;
  created_ts: string; decided_ts: string | null; decided_by: string | null; result: { message?: string } | null;
  proposal: { confidence: number; params: Record<string, unknown> };
}

export interface Cluster {
  cluster_id: string; primary_system: string; signature: string; systems: string[]; alert_count: number;
  max_severity: string; status: string; classification: string; confidence: number; incident_id: string | null;
  ack_latency_s: number | null; ack_real_ms: number | null; first_alert_ts: string; last_alert_ts: string;
  tier: number | null; rationale: string | null; truth_tp: number;
}

export interface Alert {
  alert_id: string; alert_name: string; source_system: string; severity: string; alert_ts: string;
  status: string; cluster_id: string | null; classification: string | null;
}

export interface LiveFeed {
  raw_alerts: number; cluster_count: number; clusters: Cluster[]; alerts: Alert[];
  status_counts: Record<string, number>; sim_now: string;
  service_requests: SR[]; sr_count: number;
}

export interface RunRow { run_id: string; scenario: string; title: string; speed: number; status: string; total_events: number; done_events: number; duration_sim_s: number }

/** A dummy notice (mock email) the backend recorded; shown as a pop-up. */
export interface Notice { id: string; incident_id: string; kind: "resolved" | "assigned"; subject: string; body: string; to: string[]; sent_ts: string }

export interface Overview {
  clock: { sim_now: string; speed: number; paused: boolean };
  run: RunRow | null;
  surge_mode: boolean; triage_hold: boolean; llm_mode: "rules" | "live";
  kill_switch: { global: boolean; agents: Record<string, boolean>; apps: Record<string, boolean> };
  sla: { met_pct: number | null; incidents_met: number; incidents_closed: number; requests_met: number;
         requests_closed: number; open_overdue: number };
  incidents: { open_by_priority: Record<string, number>; open_total: number; at_risk: number; resolved: number; auto_resolved: number; with_person: number; historical_sla_pct: number | null };
  alerts: { raw: number; clusters: number; suppressed_clusters: number; noise_alerts: number; classified_correctly: number;
            real_suppressed: number; raw_noise_pct: number | null;
            surfaced_noise_pct: number | null; ack_avg_s: number | null; ack_avg_real_ms: number | null };
  sr: { by_status: Record<string, number>; completed: number; auto_fulfilled: number; pending_human: number; pending_approval: number };
  kb: { articles: number; mean_score: number | null; mean_completeness: number | null; below_threshold: number; threshold: number; pending_drafts: number };
  approvals: { pending: number; blocked: number; executed: number; rejected: number };
  llm: { calls: number; input_tokens: number; output_tokens: number; cache_read_tokens: number };
  pending_events: number;
}

export interface ScenarioInfo { name: string; title: string; description: string; duration_s: number; default_speed: number; events: number; demo_items: string }
export interface ScenarioStatus { active: boolean; paused: boolean; run: RunRow | null; sim_now: string; speed: number; emitted: number; total: number | null; scenario: string | null }

export interface SR {
  sr_id: string; request_type: string | null; requestor_id: string; status: string; channel: string; text: string | null;
  target_system: string | null; access_level: string | null; fulfilled_by: string | null; fulfillment_time_hrs: number | null;
  sla_target_hrs: number | null; sla_met: string | null; outcome: string | null; submitted_ts: string;
  trace: { ts: string; text: string }[];
}

export interface Employee { employee_id: string; name: string; role: string; department: string; status: string }

export interface KbArticle {
  article_id: string; title: string; category: string; last_updated: string; usage_count_90days: number;
  escalation_despite_kb: number; score: number | null; recommendation: "keep" | "update" | "archive" | null;
  issues: string[]; rationale: string | null; score_parts: { freshness: number; escalation: number; deprecated: number; days_since_update: number } | null;
  updated_by_agent: number; superseded_by: string | null; below_threshold: boolean; pending_draft_id: number | null;
  seed_accuracy_pct: number | null; systems: string[];
}
export interface KbDraft {
  draft_id: number; status: string; change_summary: string; mode: string; sources: string[];
  diff: { op: "+" | "-" | " "; text: string }[]; approval: { approval_id: number; status: string } | null;
}
export interface KbDetail extends KbArticle { body: string; drafts: KbDraft[] }

export interface Kpi {
  key: string; label: string; unit: string; baseline: number; target: number; better: "lower" | "higher";
  target_text: string; after: number; kind: "measured" | "modelled"; method: string; detail: Record<string, unknown>;
  target_met: boolean; after_display: string | null; goal: string;
}
export interface RunKpis { run: RunRow | null; kpis: Kpi[] }

export interface SettingsPayload {
  kill_switch: { global: boolean; agents: Record<string, boolean>; apps: Record<string, boolean> };
  llm_mode: "rules" | "live"; llm_mode_env: string; surge_mode: boolean; triage_hold: boolean; model: string;
  policy: Record<string, unknown>; apps: { app_id: string; tier: number }[];
}
export interface LlmUsage {
  mode: string; model: string;
  by_agent: { agent: string; calls: number; input_tokens: number; output_tokens: number; cache_read: number; avg_ms: number; errors: number }[];
  recent: { id: number; ts: string; agent: string; schema: string; effort: string; latency_ms: number; stop_reason: string | null; error: string | null }[];
}

// ---- audit trail (ADR-0004) -------------------------------------------------------------------
export type SpanType = "node" | "llm" | "tool" | "guardrail" | "human" | "orchestrator";

export interface TraceRow {
  trace_id: string; run_id: string | null; event_id: number | null; event_type: string | null; entity_ref: string | null;
  started_sim: string; agents: string[]; steps: number; tool_calls: number; llm_calls: number; retries: number; issues: number;
  latency_ms: number | null; input_tokens: number; output_tokens: number; cache_read: number; cost_usd: number;
  accuracy: { n: number; correct: number } | null;
}

export interface AuditSpan {
  span_id: string; trace_id: string; parent_span_id: string | null; agent: string | null; span_type: SpanType; name: string;
  iteration: number | null; attempt: number | null; status: string; error: string | null; decision_id: number | null;
  entity_ref: string | null; started_sim: string; offset_ms: number; latency_ms: number;
  input: Record<string, unknown> | null; output: unknown;
}

export interface LlmCall {
  id: number; agent: string; schema: string; effort: string; model: string | null; attempt: number | null;
  input_tokens: number | null; output_tokens: number | null; cache_read: number | null; cache_write: number | null;
  cost_usd: number | null; latency_ms: number; stop_reason: string | null; error: string | null;
  thinking_summary: string | null; span_id: string | null;
}

export interface AccuracyRow {
  id: number; agent: string; metric: string; predicted: string | null; expected: string | null; correct: number | null;
  score: number | null; confidence: number | null; source: string; mode: string | null; decision_id: number | null;
}

export interface TraceDetail {
  trace_id: string; event: { event_id: number; type: string; entity_ref: string; ts_sim: string; status: string; error: string | null } | null;
  spans: AuditSpan[]; decisions: Decision[]; llm_calls: LlmCall[]; accuracy: AccuracyRow[];
}

export interface AgentMetrics {
  iteration: { traces: number; node_runs: number; avg_steps: number; max_steps: number; llm_attempts: number; retries: number; errors: number };
  tools: { calls: number; errors: number; by_tool: { tool: string; calls: number; errors: number; p50_ms: number | null; p95_ms: number | null }[] };
  tokens: { calls: number; input: number; output: number; cache_read: number; cache_write: number; cost_usd: number; errors: number };
  accuracy: { graded: number; accuracy_pct: number | null; by_source: Record<string, { n: number; correct: number; accuracy_pct: number }>;
              calibration: { bucket: string; n: number; avg_confidence: number; accuracy_pct: number }[]; kb_scores: number[] };
  activity: { decisions: number; by_kind: Record<string, number>; guardrail_verdicts: Record<string, number>; human_decisions: number; override_rate_pct: number | null };
  latency: Record<string, { n: number; p50_ms: number | null; p95_ms: number | null; max_ms: number }>;
}

export interface AuditMetrics {
  filters: { run_id: string | null; agent: string | null };
  agents: Record<string, AgentMetrics>; totals: AgentMetrics;
  batch_evals: { triage?: { n: number; category_acc_pct: number; priority_within_1_pct: number; modes?: string[] };
                 alert_classification?: { n: number; accuracy_pct: number } } | null;
}
