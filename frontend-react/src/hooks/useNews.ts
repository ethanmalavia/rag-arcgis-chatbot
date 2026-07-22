import { useCallback, useEffect, useState } from "react";
import type { NewsPost } from "../types";

const NEWS_URL = "https://esterotoday.com/wp-json/wp/v2/posts?per_page=5&_fields=id,title,link,date,categories";

interface WpPost {
  id: number;
  date: string;
  link: string;
  title: { rendered: string };
}

interface UseNewsResult {
  posts: NewsPost[];
  loading: boolean;
  error: string | null;
  retry: () => void;
}

function decodeHtmlEntities(text: string): string {
  const el = document.createElement("textarea");
  el.innerHTML = text;
  return el.value;
}

/** Fetches the 5 newest EsteroToday posts directly from their public WordPress
 * REST API — confirmed to send permissive CORS headers (reflects Origin), so
 * no backend proxy is needed. If that ever changes, this is the one place to
 * swap the URL for a backend proxy endpoint. */
export function useNews(): UseNewsResult {
  const [posts, setPosts] = useState<NewsPost[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  const retry = useCallback(() => setAttempt((a) => a + 1), []);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);

    fetch(NEWS_URL)
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json() as Promise<WpPost[]>;
      })
      .then((data) => {
        if (cancelled) return;
        setPosts(
          data.map((p) => ({
            id: p.id,
            title: decodeHtmlEntities(p.title.rendered),
            link: p.link,
            date: p.date,
          })),
        );
        setLoading(false);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : "Failed to load news");
        setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [attempt]);

  return { posts, loading, error, retry };
}
