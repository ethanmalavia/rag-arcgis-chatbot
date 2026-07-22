import { useCallback, useEffect, useRef } from "react";

// Sticks to the bottom while new content streams in, but stops fighting
// the user the moment they scroll up to read earlier messages. Once they
// scroll back near the bottom themselves, auto-scroll resumes.
export function useAutoScroll<T extends HTMLElement>(deps: unknown[]) {
  const ref = useRef<T | null>(null);
  const stickToBottomRef = useRef(true);

  const onScroll = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    stickToBottomRef.current = distanceFromBottom < 80;
  }, []);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (stickToBottomRef.current) {
      el.scrollTop = el.scrollHeight;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return { ref, onScroll };
}
