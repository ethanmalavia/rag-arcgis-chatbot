import { useCallback, useEffect, useRef, useState } from "react";
import { API_BASE } from "../lib/config";

export type HealthStatus = "loading" | "online" | "offline";

export function useHealth(intervalMs = 15000) {
  const [status, setStatus] = useState<HealthStatus>("loading");
  const [indexLoaded, setIndexLoaded] = useState<boolean | null>(null);
  const timerRef = useRef<number | undefined>(undefined);

  const check = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/health`);
      if (!res.ok) throw new Error(String(res.status));
      const data = await res.json();
      setStatus("online");
      setIndexLoaded(!!data.index_loaded);
    } catch {
      setStatus("offline");
      setIndexLoaded(null);
    }
  }, []);

  useEffect(() => {
    check();
    timerRef.current = window.setInterval(check, intervalMs);
    return () => window.clearInterval(timerRef.current);
  }, [check, intervalMs]);

  return { status, indexLoaded, retry: check };
}
