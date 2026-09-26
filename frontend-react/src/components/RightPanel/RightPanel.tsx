import { useEffect, useState } from "react";
import MapPanel from "../MapPanel/MapPanel";
import Dashboard from "../Dashboard/Dashboard";
import { useMeetings } from "../../hooks/useMeetings";
import { registerTabSwitcher } from "../../lib/uiStore";
import { captureViewState, restoreViewState } from "../../lib/mapViewStore";
import type { RightTab } from "../../types";

interface RightPanelProps {
  expanded: boolean;
  onToggleExpand: () => void;
  mobileVisible: boolean;
  onRecordCount: (count: number | null) => void;
  onSend: (text: string) => void;
}

/** Map | Pulse tabs. MapPanel stays mounted at all times (display:none when
 * hidden, never unmounted) — re-initializing the ArcGIS MapView is expensive
 * and would lose pan/zoom state, per the design brief. */
export default function RightPanel({ expanded, onToggleExpand, mobileVisible, onRecordCount, onSend }: RightPanelProps) {
  const [activeTab, setActiveTab] = useState<RightTab>("map");
  const { meetings, loading: meetingsLoading, error: meetingsError, hasUpcomingWithinWeek } = useMeetings(3);

  useEffect(() => {
    // Raw setter, not handleTabChange — external callers (the chat card's
    // "Show on map" link) want a plain switch, since they pan to a specific
    // destination themselves right after and don't want the old view restored
    // over top of it.
    registerTabSwitcher(setActiveTab);
    return () => registerTabSwitcher(null);
  }, []);

  // Local tab-button clicks capture/restore map view state explicitly: toggling
  // the MapView's container through display:none <-> display:flex (so the
  // expensive WebGL view survives instead of being destroyed) isn't something
  // ArcGIS's own resize handling reliably preserves center/zoom across.
  const handleTabChange = (tab: RightTab) => {
    if (tab === activeTab) return;
    if (activeTab === "map") captureViewState();
    setActiveTab(tab);
    if (tab === "map") {
      requestAnimationFrame(() => requestAnimationFrame(() => restoreViewState()));
    }
  };

  return (
    <div id="right-panel" className={mobileVisible ? "mobile-show" : ""}>
      <div className="right-tabs" role="tablist" aria-label="Map and Community Pulse">
        <span className={`right-tab-indicator right-tab-indicator-${activeTab}`} aria-hidden="true" />
        <button
          type="button"
          role="tab"
          aria-selected={activeTab === "map"}
          className={`right-tab ${activeTab === "map" ? "active" : ""}`}
          onClick={() => handleTabChange("map")}
        >
          🗺 Map
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={activeTab === "pulse"}
          className={`right-tab ${activeTab === "pulse" ? "active" : ""}`}
          onClick={() => handleTabChange("pulse")}
        >
          Pulse
          {hasUpcomingWithinWeek && <span className="pulse-notify-dot" aria-label="Meeting this week" />}
        </button>
      </div>

      {/* Both panes stay mounted at all times (never unmounted) — re-initializing
          the map iframe is expensive and loses pan/zoom state (see the file-level
          comment above). Visibility is opacity/pointer-events, not display:none,
          so the two panes can crossfade instead of hard-swapping. */}
      <div className="right-tab-panes">
        <div className={`right-tab-content right-tab-pane ${activeTab === "map" ? "active" : ""}`}>
          <MapPanel expanded={expanded} onToggleExpand={onToggleExpand} onRecordCount={onRecordCount} />
        </div>
        <div
          className={`right-tab-content right-tab-content-scroll right-tab-pane ${activeTab === "pulse" ? "active" : ""}`}
        >
          <Dashboard meetings={meetings} meetingsLoading={meetingsLoading} meetingsError={meetingsError} onSend={onSend} />
        </div>
      </div>
    </div>
  );
}
