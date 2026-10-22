import type * as T from "./types";

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

async function req<R>(path: string, init?: RequestInit): Promise<R> {
  const res = await fetch(`/api${path}`, {
    ...init,
    headers: init?.body ? { "content-type": "application/json", ...init.headers } : init?.headers,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail ?? detail; } catch { /* non-JSON error body */ }
    throw new ApiError(res.status, typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return res.json() as Promise<R>;
}
const post = <R>(path: string, body?: unknown) => req<R>(path, { method: "POST", body: JSON.stringify(body ?? {}) });
const qs = (p: Record<string, string | number | undefined>) => {
  const s = Object.entries(p).filter(([, v]) => v !== undefined && v !== "").map(([k, v]) => `${k}=${encodeURIComponent(String(v))}`).join("&");
  return s ? `?${s}` : "";
};

export const api = {
  overview: () => req<T.Overview>("/overview"),
  notifications: () => req<T.Notice[]>("/notifications"),
  scenarios: () => req<{ scenarios: T.ScenarioInfo[]; status: T.ScenarioStatus }>("/scenarios"),
  scenarioStatus: () => req<T.ScenarioStatus>("/scenarios/status"),
  startScenario: (name: string, speed?: number) => post<T.ScenarioStatus>(`/scenarios/${name}/start`, { speed }),
  pauseScenario: () => post<T.ScenarioStatus>("/scenarios/pause"),
  resumeScenario: () => post<T.ScenarioStatus>("/scenarios/resume"),
  stopScenario: () => post<T.ScenarioStatus>("/scenarios/stop"),
  setSpeed: (speed: number) => post<T.ScenarioStatus>("/scenarios/speed", { speed }),
  liveFeed: () => req<T.LiveFeed>("/live-feed"),
  incidents: (status?: string) => req<T.Incident[]>(`/incidents${qs({ status })}`),
  incident: (id: string) => req<T.Incident & {
    cluster: T.Cluster | null; similar: { incident_id: string; short_description: string; category: string; priority: string; assigned_team: string }[];
    kb: { article_id: string; title: string; score: number }[]; kb_excluded: { article_id: string; title: string; score: number | null }[];
    decisions: T.Decision[]; approvals: T.Approval[];
  }>(`/incidents/${id}`),
  approvals: (status?: string) => req<T.Approval[]>(`/approvals${qs({ status })}`),
  decide: (id: number, approve: boolean, note = "") => post<{ state: string; result: { message?: string } }>(`/approvals/${id}/decide`, { approve, decided_by: "svc.delivery.mgr", note }),
  feed: (limit = 60) => req<T.Decision[]>(`/feed${qs({ limit })}`),
  decisions: (entity: string) => req<T.Decision[]>(`/decisions/${entity}`),
  employees: () => req<T.Employee[]>("/employees"),
  selfService: (body: Record<string, unknown>) => post<{ sr: T.SR; approvals: T.Approval[]; decisions: T.Decision[]; surge_mode: boolean }>("/self-service", body),
  serviceRequests: (limit = 40) => req<T.SR[]>(`/service-requests${qs({ limit })}`),
  srSummary: () => req<{ total: number; by_status: Record<string, number>; auto: number; human: number; completed: number; sla_met: number; surge_mode: boolean;
                         opportunity: { request_type: string; avg_volume_per_month: number; automation_potential: string; est_fte_savings: number }[] }>("/service-requests/summary"),
  kbArticles: () => req<T.KbArticle[]>("/kb/articles"),
  kbArticle: (id: string) => req<T.KbDetail>(`/kb/articles/${id}`),
  kbReview: () => post<{ queued: boolean }>("/kb/review", {}),
  kbDraft: (id: string) => post<{ queued: boolean }>(`/kb/articles/${id}/draft`),
  kpiRun: () => req<T.RunKpis>("/kpis/run"),
  settings: () => req<T.SettingsPayload>("/settings"),
  killSwitch: (body: Record<string, unknown>) => post<unknown>("/settings/kill-switch", body),
  llmUsage: () => req<T.LlmUsage>("/llm/usage"),
  llmHealth: () => post<{ ok: boolean; error?: string; text?: string; mode: string }>("/llm/health-check"),
  advance: (minutes: number) => post<{ sim_now: string; sla_checks_queued: number }>("/sim/advance", { minutes }),
  reset: () => post<unknown>("/admin/reset"),
  graph: () => req<{ mermaid: string }>("/graph"),
  auditTraces: (p: { run_id?: string; agent?: string; entity?: string; status?: string; limit?: number }) => req<T.TraceRow[]>(`/audit/traces${qs(p)}`),
  auditTrace: (id: string) => req<T.TraceDetail>(`/audit/traces/${encodeURIComponent(id)}`),
  auditMetrics: (p: { run_id?: string; agent?: string }) => req<T.AuditMetrics>(`/audit/metrics${qs(p)}`),
  auditExportUrl: (table: string, run_id?: string, format = "csv") => `/api/audit/export${qs({ table, run_id, format })}`,
};
