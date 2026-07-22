import { useCallback, useState } from "react";
import TopBar from "./components/TopBar/TopBar";
import ChatPanel from "./components/ChatPanel/ChatPanel";
import RightPanel from "./components/RightPanel/RightPanel";
import ReportDialog, { type ReportPrefill } from "./components/ReportDialog/ReportDialog";
import { useChat } from "./hooks/useChat";
import { useHealth } from "./hooks/useHealth";
import "./App.css";

export default function App() {
  const { messages, send, newChat } = useChat();
  const { status: healthStatus, retry: retryHealth } = useHealth();
  const [recordCount, setRecordCount] = useState<number | null | undefined>(undefined);
  const [mapExpanded, setMapExpanded] = useState(false);
  const [mobileMapVisible, setMobileMapVisible] = useState(false);
  const [reportOpen, setReportOpen] = useState(false);
  const [reportPrefill, setReportPrefill] = useState<ReportPrefill | null>(null);

  const handleRecordCount = useCallback((count: number | null) => setRecordCount(count), []);
  const toggleExpand = useCallback(() => setMapExpanded((v) => !v), []);
  const toggleMobileMap = useCallback(() => setMobileMapVisible((v) => !v), []);

  const openReport = useCallback((prefill?: ReportPrefill) => {
    setReportPrefill(prefill || null);
    setReportOpen(true);
  }, []);

  const busy = messages.some((m) => m.role === "bot" && m.streaming);

  return (
    <>
      <TopBar
        recordCount={recordCount}
        healthStatus={healthStatus}
        onRetryHealth={retryHealth}
        onNewChat={newChat}
        onToggleMobileMap={toggleMobileMap}
        onReport={() => openReport()}
      />
      <div id="app" className={mapExpanded ? "map-expanded" : ""}>
        <ChatPanel messages={messages} onSend={send} disabled={busy} onReport={openReport} />
        <RightPanel
          expanded={mapExpanded}
          onToggleExpand={toggleExpand}
          mobileVisible={mobileMapVisible}
          onRecordCount={handleRecordCount}
          onSend={send}
        />
      </div>
      <ReportDialog open={reportOpen} onClose={() => setReportOpen(false)} prefill={reportPrefill} />
    </>
  );
}
