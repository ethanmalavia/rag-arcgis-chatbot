import { useCallback, useEffect, useState } from "react";
import { API_BASE } from "../lib/config";
import type { RecentDecision } from "../types";

interface ApiDecision {
  title: string;
  date: string | null;
  board: string | null;
  status: string | null;
  application_id: string | null;
}

interface UseRecentDecisionsResult {
  decisions: RecentDecision[];
  loading: boolean;
  error: string | null;
  retry: () => void;
}

export function useRecentDecisions(): UseRecentDecisionsResult {
  const [decisions, setDecisions] = useState<RecentDecision[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  const retry = useCallback(() => setAttempt((a) => a + 1), []);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);

    fetch(`${API_BASE}/recent-decisions`)
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json() as Promise<{ decisions: ApiDecision[] }>;
      })
      .then((data) => {
        if (cancelled) return;
        setDecisions(
          (data.decisions || []).map((d) => ({
            title: d.title,
            date: d.date,
            board: d.board,
            status: d.status,
            applicationId: d.application_id,
          })),
        );
        setLoading(false);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : "Failed to load recent decisions");
        setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [attempt]);

  return { decisions, loading, error, retry };
}
