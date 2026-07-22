import { useEffect, useState } from "react";
import type { Meeting } from "../types";

interface MeetingsFile {
  meetings: Meeting[];
}

interface UseMeetingsResult {
  meetings: Meeting[];
  loading: boolean;
  error: string | null;
  hasUpcomingWithinWeek: boolean;
}

/** True if dateStr (YYYY-MM-DD) falls within the next `days` days, inclusive of today. */
export function isWithinDays(dateStr: string, days: number): boolean {
  const target = new Date(`${dateStr}T00:00:00`);
  const now = new Date();
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const diffDays = (target.getTime() - startOfToday.getTime()) / 86_400_000;
  return diffDays >= 0 && diffDays <= days;
}

export function meetingIsThisWeek(dateStr: string): boolean {
  return isWithinDays(dateStr, 7);
}

/** Loads public/meetings.json (manually maintained — see that file's header
 * comment) and filters to upcoming occurrences only, so the widget stays
 * accurate between manual updates instead of showing stale past dates. */
export function useMeetings(limit = 3): UseMeetingsResult {
  const [meetings, setMeetings] = useState<Meeting[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);

    fetch("/meetings.json")
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json() as Promise<MeetingsFile>;
      })
      .then((data) => {
        if (cancelled) return;
        const todayStr = new Date().toISOString().slice(0, 10);
        const upcoming = (data.meetings || [])
          .filter((m) => m.date >= todayStr)
          .sort((a, b) => a.date.localeCompare(b.date))
          .slice(0, limit);
        setMeetings(upcoming);
        setLoading(false);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : "Failed to load meetings");
        setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [limit]);

  const hasUpcomingWithinWeek = meetings.some((m) => meetingIsThisWeek(m.date));

  return { meetings, loading, error, hasUpcomingWithinWeek };
}
