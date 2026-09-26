import type { KeyboardEvent, PointerEvent } from "react";

interface SplitDividerProps {
  fraction: number;
  dragging: boolean;
  onPointerDown: (e: PointerEvent<HTMLDivElement>) => void;
  onDoubleClick: () => void;
  onKeyDown: (e: KeyboardEvent<HTMLDivElement>) => void;
  onCollapse: () => void;
}

/** Draggable divider between the chat panel and the Map/Pulse panel. Stays
 * mounted and interactive whenever the right panel isn't collapsed — once
 * collapsed, its grid column shrinks to 0 width (see App.css) and the
 * separate floating "Map / Pulse" tab in App.tsx takes over reopening. */
export default function SplitDivider({
  fraction,
  dragging,
  onPointerDown,
  onDoubleClick,
  onKeyDown,
  onCollapse,
}: SplitDividerProps) {
  return (
    <div
      className={`split-divider${dragging ? " dragging" : ""}`}
      role="separator"
      aria-orientation="vertical"
      aria-label="Resize chat and Map/Pulse panels"
      aria-valuenow={Math.round(fraction * 100)}
      aria-valuemin={0}
      aria-valuemax={100}
      tabIndex={0}
      onPointerDown={onPointerDown}
      onDoubleClick={onDoubleClick}
      onKeyDown={onKeyDown}
    >
      <div className="split-grip" aria-hidden="true">
        <span />
        <span />
        <span />
      </div>
      <button
        type="button"
        className="split-collapse-btn"
        title="Collapse Map / Pulse panel"
        aria-label="Collapse Map / Pulse panel"
        onPointerDown={(e) => e.stopPropagation()}
        onClick={(e) => {
          e.stopPropagation();
          onCollapse();
        }}
      >
        ❯
      </button>
    </div>
  );
}
