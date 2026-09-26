import { useCallback, useEffect, useRef, useState } from "react";
import type { KeyboardEvent, PointerEvent } from "react";

const STORAGE_KEY = "estero_split_panel";
const CHAT_MIN_PX = 360;
const RIGHT_MIN_PX = 320;
const SNAP_FRACTIONS = [0.3, 0.5, 0.7];
const SNAP_THRESHOLD = 0.03;
const KEYBOARD_STEP = 0.05;
const DEFAULT_FRACTION = 0.46; // matches the old fixed 46% / 1fr split

interface PersistedSplit {
  fraction: number;
  collapsed: boolean;
}

function loadPersisted(): PersistedSplit | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<PersistedSplit>;
    if (typeof parsed.fraction === "number" && typeof parsed.collapsed === "boolean") {
      return { fraction: parsed.fraction, collapsed: parsed.collapsed };
    }
    return null;
  } catch {
    return null;
  }
}

function savePersisted(state: PersistedSplit): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  } catch {
    // localStorage unavailable (private mode, quota, etc.) — state still
    // works for this session, it just won't be remembered next visit.
  }
}

/** Nudge anything listening for window resizes (notably the Map tab's Esri
 * iframe, which has no JS-SDK view to call .resize()/.invalidateSize() on —
 * it's a plain cross-origin iframe that already reflows with its container
 * via CSS, but this gives its own internal app a resize event to react to
 * too). Delayed slightly so it fires after the width transition settles. */
function nudgeResize(delay = 0): void {
  window.setTimeout(() => window.dispatchEvent(new Event("resize")), delay);
}

export function useSplitPanel() {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const persisted = useRef(loadPersisted()).current;

  const [fraction, setFraction] = useState(persisted?.fraction ?? DEFAULT_FRACTION);
  const [collapsed, setCollapsed] = useState(persisted?.collapsed ?? false);
  const [dragging, setDragging] = useState(false);

  const clampFraction = useCallback((f: number) => {
    const total = containerRef.current?.getBoundingClientRect().width || 1000;
    const minFrac = CHAT_MIN_PX / total;
    const maxFrac = 1 - RIGHT_MIN_PX / total;
    if (minFrac > maxFrac) return 0.5;
    return Math.min(maxFrac, Math.max(minFrac, f));
  }, []);

  // Re-clamp on viewport changes so a percentage split doesn't drift below
  // the pixel minimums after the window is resized (no active drag needed).
  useEffect(() => {
    const onResize = () => setFraction((f) => clampFraction(f));
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [clampFraction]);

  const moveTo = useCallback(
    (clientX: number) => {
      const rect = containerRef.current?.getBoundingClientRect();
      if (!rect || rect.width === 0) return;
      setFraction(clampFraction((clientX - rect.left) / rect.width));
    },
    [clampFraction],
  );

  const onPointerDown = useCallback(
    (e: PointerEvent<HTMLDivElement>) => {
      if (collapsed) return;
      e.currentTarget.setPointerCapture(e.pointerId);
      setDragging(true);
    },
    [collapsed],
  );

  useEffect(() => {
    if (!dragging) return;

    const onMove = (e: globalThis.PointerEvent) => moveTo(e.clientX);
    const onUp = (e: globalThis.PointerEvent) => {
      setDragging(false);
      const rect = containerRef.current?.getBoundingClientRect();
      const raw = rect && rect.width > 0 ? (e.clientX - rect.left) / rect.width : fraction;
      const clamped = clampFraction(raw);
      const snapped = SNAP_FRACTIONS.find((s) => Math.abs(clamped - s) < SNAP_THRESHOLD);
      const finalFraction = snapped ?? clamped;
      setFraction(finalFraction);
      savePersisted({ fraction: finalFraction, collapsed: false });
      nudgeResize(220); // after the ease-into-snap transition finishes
    };

    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dragging]);

  const onDoubleClick = useCallback(() => {
    setFraction(0.5);
    setCollapsed(false);
    savePersisted({ fraction: 0.5, collapsed: false });
    nudgeResize(220);
  }, []);

  const onKeyDown = useCallback(
    (e: KeyboardEvent<HTMLDivElement>) => {
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      e.preventDefault();
      const delta = e.key === "ArrowLeft" ? -KEYBOARD_STEP : KEYBOARD_STEP;
      setFraction((f) => {
        const next = clampFraction(f + delta);
        savePersisted({ fraction: next, collapsed: false });
        return next;
      });
      nudgeResize(220);
    },
    [clampFraction],
  );

  const toggleCollapsed = useCallback(() => {
    setCollapsed((c) => {
      const next = !c;
      savePersisted({ fraction, collapsed: next });
      nudgeResize(320); // after the grid-template-columns transition finishes
      return next;
    });
  }, [fraction]);

  return {
    containerRef,
    fraction,
    collapsed,
    dragging,
    onPointerDown,
    onDoubleClick,
    onKeyDown,
    toggleCollapsed,
  };
}
