import { useCallback, useState } from "react";
import TopBar from "./components/TopBar/TopBar";
import ChatPanel from "./components/ChatPanel/ChatPanel";
import RightPanel from "./components/RightPanel/RightPanel";
import SplitDivider from "./components/SplitDivider/SplitDivider";
import ReportDialog, { type ReportPrefill } from "./components/ReportDialog/ReportDialog";
import DisclaimerModal from "./components/DisclaimerModal/DisclaimerModal";
import { useChat } from "./hooks/useChat";
import { useSplitPanel } from "./hooks/useSplitPanel";
import type { MapPanelView } from "./components/MapPanel/MapPanel";
import "./App.css";

const STORY_MIN_WIDTH_PX = 1100;

export default function App() {
  const { messages, send, newChat } = useChat();
  const [recordCount, setRecordCount] = useState<number | null | undefined>(undefined);
  const [mapExpanded, setMapExpanded] = useState(false);
  const [mobileMapVisible, setMobileMapVisible] = useState(false);
  const [reportOpen, setReportOpen] = useState(false);
  const [reportPrefill, setReportPrefill] = useState<ReportPrefill | null>(null);
  const split = useSplitPanel();

  const handleRecordCount = useCallback((count: number | null) => setRecordCount(count), []);
  const toggleExpand = useCallback(() => setMapExpanded((v) => !v), []);
  const toggleMobileMap = useCallback(() => setMobileMapVisible((v) => !v), []);

  // The StoryMap swaps its inline dashboards for link cards when its iframe is
  // too narrow, so widen the right panel while it's shown and put the user's
  // split back when they switch to the map. 1100px is a width verified to show
  // the dashboards inline; the exact cutoff is Esri's and undocumented.
  const { widenRightTo, restoreRightWidth } = split;
  const handleMapViewChange = useCallback(
    (view: MapPanelView) => {
      if (view === "story") widenRightTo(STORY_MIN_WIDTH_PX);
      else restoreRightWidth();
    },
    [widenRightTo, restoreRightWidth],
  );

  const openReport = useCallback((prefill?: ReportPrefill) => {
    setReportPrefill(prefill || null);
    setReportOpen(true);
  }, []);

  const busy = messages.some((m) => m.role === "bot" && m.streaming);

  const appClassName = [
    mapExpanded && "map-expanded",
    split.dragging && "dragging",
    split.collapsed && "right-collapsed",
  ]
    .filter(Boolean)
    .join(" ");

  // #app.map-expanded already forces a single 1fr column via CSS (and the
  // divider isn't rendered in that mode, see below) — an inline style here
  // would win specificity over that stylesheet rule, so only set it when the
  // split panel is actually in control of the layout.
  const appStyle = mapExpanded
    ? undefined
    : {
        gridTemplateColumns: split.collapsed
          ? "1fr 0px 0px"
          : `${split.fraction}fr 6px ${1 - split.fraction}fr`,
      };

  return (
    <>
      <DisclaimerModal />
      <TopBar recordCount={recordCount} onToggleMobileMap={toggleMobileMap} />
      <div id="app" ref={split.containerRef} className={appClassName} style={appStyle}>
        <ChatPanel messages={messages} onSend={send} disabled={busy} onReport={openReport} onNewChat={newChat} />
        {!mapExpanded && (
          <SplitDivider
            fraction={split.fraction}
            dragging={split.dragging}
            onPointerDown={split.onPointerDown}
            onDoubleClick={split.onDoubleClick}
            onKeyDown={split.onKeyDown}
            onCollapse={split.toggleCollapsed}
          />
        )}
        <RightPanel
          expanded={mapExpanded}
          onToggleExpand={toggleExpand}
          mobileVisible={mobileMapVisible}
          onRecordCount={handleRecordCount}
          onMapViewChange={handleMapViewChange}
          onSend={send}
        />
      </div>
      {!mapExpanded && split.collapsed && (
        <button type="button" className="split-reopen-tab" onClick={split.toggleCollapsed}>
          Map / Pulse
        </button>
      )}
      <ReportDialog open={reportOpen} onClose={() => setReportOpen(false)} prefill={reportPrefill} />
    </>
  );
}
