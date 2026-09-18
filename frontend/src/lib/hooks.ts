import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Poll an endpoint.
 *
 * - Keeps the last good data while refetching (no flashes); resets when
 *   ``deps`` change (a different entity must not show the previous one's data).
 * - Applies a response only if it is newer than the last one applied, so a
 *   slow response can never overwrite a fresher one, and responses slower
 *   than the interval still land.
 * - One request at a time per poller.
 * - Slows down (5x) instead of stopping while the page is hidden: browsers
 *   also report fully covered windows as hidden, and an ops console on a
 *   covered screen must not freeze on old data.
 */
export function usePoll<T>(fetcher: () => Promise<T>, intervalMs: number, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const fetchRef = useRef(fetcher);
  fetchRef.current = fetcher;
  const issued = useRef(0);
  const applied = useRef(0);
  const inFlight = useRef(false);

  const refresh = useCallback(async () => {
    const mine = ++issued.current;
    inFlight.current = true;
    try {
      const d = await fetchRef.current();
      if (mine > applied.current) {
        applied.current = mine;
        setData(d);
        setError(null);
      }
    } catch (e) {
      if (mine > applied.current) setError(e);
    } finally {
      if (mine === issued.current) inFlight.current = false;
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    applied.current = issued.current; // drop anything still in flight for the previous deps
    setData(null);
    setLoading(true);
    refresh();
    if (!intervalMs) return;
    let last = Date.now();
    const timer = window.setInterval(() => {
      const every = document.visibilityState === "visible" ? intervalMs : intervalMs * 5;
      if (inFlight.current || Date.now() - last < every - 50) return;
      last = Date.now();
      refresh();
    }, intervalMs);
    const onVisible = () => {
      if (document.visibilityState === "visible") {
        last = Date.now();
        refresh();
      }
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, refresh, ...deps]);

  return { data, error, loading, refresh };
}

/** Width of an element, tracked with ResizeObserver. */
export function useWidth<T extends HTMLElement>(): [React.RefObject<T | null>, number] {
  const ref = useRef<T | null>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(([entry]) => setWidth(Math.floor(entry.contentRect.width)));
    ro.observe(el);
    setWidth(Math.floor(el.getBoundingClientRect().width));
    return () => ro.disconnect();
  }, []);
  return [ref, width];
}

/** Re-render every ``ms`` (for "12s ago" labels and live elapsed timers). */
export function useNow(ms = 1000): Date {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const t = window.setInterval(() => setNow(new Date()), ms);
    return () => window.clearInterval(t);
  }, [ms]);
  return now;
}

export type ThemePref = "dark" | "light" | "system";

export function useTheme(): [ThemePref, (p: ThemePref) => void] {
  const [pref, setPref] = useState<ThemePref>(() => {
    try {
      return (localStorage.getItem("opsforge-theme") as ThemePref) || "dark";
    } catch {
      return "dark";
    }
  });
  useEffect(() => {
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const apply = () => {
      const dark = pref === "dark" || (pref === "system" && media.matches);
      document.documentElement.dataset.theme = dark ? "dark" : "light";
    };
    apply();
    try {
      localStorage.setItem("opsforge-theme", pref);
    } catch {
      /* storage unavailable: the choice lasts for this page only */
    }
    media.addEventListener("change", apply);
    return () => media.removeEventListener("change", apply);
  }, [pref]);
  return [pref, setPref];
}
