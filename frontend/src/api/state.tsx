import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { api } from "./client";
import type { Overview, ScenarioStatus } from "./types";
import { toaster } from "../components/ui/toaster";

/** Poll a loader; keeps the previous value while refreshing so tables don't flicker. */
export function usePoll<R>(load: () => Promise<R>, ms = 2000, deps: unknown[] = []) {
  const [data, setData] = useState<R | null>(null);
  const [error, setError] = useState<string | null>(null);
  const loadRef = useRef(load);
  loadRef.current = load;
  const reload = useCallback(async () => {
    try { setData(await loadRef.current()); setError(null); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  }, []);
  useEffect(() => {
    let alive = true;
    const tick = async () => { if (alive) await reload(); };
    tick();
    const t = setInterval(tick, ms);
    return () => { alive = false; clearInterval(t); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ms, reload, ...deps]);
  return { data, error, reload };
}

interface Shell { overview: Overview | null; scenario: ScenarioStatus | null; offline: boolean; refresh: () => void }
const ShellCtx = createContext<Shell>({ overview: null, scenario: null, offline: false, refresh: () => {} });
export const useShell = () => useContext(ShellCtx);

export function ShellProvider({ children }: { children: ReactNode }) {
  const o = usePoll(api.overview, 1500);
  const s = usePoll(api.scenarioStatus, 1500);
  usePopups();
  const refresh = useCallback(() => { o.reload(); s.reload(); }, [o, s]);
  return <ShellCtx.Provider value={{ overview: o.data, scenario: s.data, offline: !!o.error && !o.data, refresh }}>{children}</ShellCtx.Provider>;
}

/** Pop up each new dummy notice (ticket resolved / assigned to a person); notices already there on load stay quiet. */
function usePopups() {
  const notices = usePoll(api.notifications, 2000);
  const seen = useRef<Set<string> | null>(null);
  useEffect(() => {
    const list = notices.data;
    if (!list) return;
    if (seen.current === null) { seen.current = new Set(list.map((n) => n.id)); return; }
    for (const n of [...list].reverse()) {
      if (seen.current.has(n.id)) continue;
      seen.current.add(n.id);
      toaster.create({
        title: n.kind === "resolved" ? `Ticket resolved: ${n.incident_id}` : `Assigned to a person: ${n.incident_id}`,
        description: `${n.body.length > 220 ? `${n.body.slice(0, 220)}…` : n.body} (mock email to ${n.to.join(", ") || "nobody"})`,
        type: n.kind === "resolved" ? "success" : "warning", duration: 10000, closable: true,
      });
    }
  }, [notices.data]);
}

type ToastTone = "ok" | "error";
const notify = (text: string, tone: ToastTone = "ok") =>
  toaster.create({ description: text, type: tone === "error" ? "error" : "success", duration: tone === "error" ? 7000 : 3500 });
export const useToast = () => notify;

/** Run an action, report the outcome as a toast, then refresh shared state. */
export function useAction() {
  const toast = useToast();
  const { refresh } = useShell();
  return useCallback(async <R,>(fn: () => Promise<R>, done?: string | ((r: R) => string)): Promise<R | undefined> => {
    try {
      const r = await fn();
      if (done) toast(typeof done === "function" ? done(r) : done);
      refresh();
      return r;
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), "error");
      return undefined;
    }
  }, [toast, refresh]);
}
