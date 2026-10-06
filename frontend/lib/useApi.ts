"use client";

import { useCallback, useEffect, useState } from "react";
import { errorMessage } from "./api";

/**
 * Loads data with a stable fetcher (module-level function or useCallback). `reload()` refetches
 * without clearing the data already shown, so polling does not flicker.
 */
export function useApi<T>(fetcher: (() => Promise<T>) | null) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    if (!fetcher) return;
    let cancelled = false;
    fetcher().then(
      (d) => {
        if (cancelled) return;
        setData(d);
        setError(null);
      },
      (err: unknown) => {
        if (!cancelled) setError(errorMessage(err));
      },
    );
    return () => {
      cancelled = true;
    };
  }, [fetcher, tick]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { data, error, reload, setData };
}

/** Calls `fn` every `ms` while `active` is true. */
export function useInterval(fn: () => void, ms: number, active: boolean) {
  useEffect(() => {
    if (!active) return;
    const t = window.setInterval(fn, ms);
    return () => window.clearInterval(t);
  }, [fn, ms, active]);
}
