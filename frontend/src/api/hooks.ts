import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "./client";

export interface Polled<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  refresh: () => void;
}

/** Poll an endpoint every `ms` (plan §7.7: 1–2 s while a scenario runs). Keeps last good data on errors. */
export function usePoll<T>(fn: () => Promise<T>, ms = 1500): Polled<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const alive = useRef(true);

  const tick = useCallback(async () => {
    try {
      const d = await fnRef.current();
      if (!alive.current) return;
      setData(d);
      setError(null);
    } catch (e) {
      if (!alive.current) return;
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      if (alive.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    alive.current = true;
    tick();
    const id = window.setInterval(() => {
      if (!document.hidden) tick();
    }, ms);
    return () => {
      alive.current = false;
      window.clearInterval(id);
    };
  }, [tick, ms]);

  return { data, error, loading, refresh: tick };
}

/** Run a command, track busy/error, then refresh the listed pollers. */
export function useAction(after: Array<() => void> = []) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = useCallback(
    async <R,>(fn: () => Promise<R>): Promise<R | undefined> => {
      setBusy(true);
      setError(null);
      try {
        const r = await fn();
        after.forEach((f) => f());
        return r;
      } catch (e) {
        setError(e instanceof ApiError ? e.message : String(e));
      } finally {
        setBusy(false);
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );
  return { run, busy, error };
}
