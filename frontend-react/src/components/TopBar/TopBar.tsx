import logo from "../../assets/logo.png";
import type { HealthStatus } from "../../hooks/useHealth";
import { API_BASE } from "../../lib/config";

interface TopBarProps {
  recordCount: number | null | undefined;
  healthStatus: HealthStatus;
  onRetryHealth: () => void;
  onNewChat: () => void;
  onToggleMobileMap: () => void;
  onReport: () => void;
}

const HEALTH_LABEL: Record<HealthStatus, string> = {
  loading: "Connecting…",
  online: "Backend online",
  offline: "Backend offline",
};

export default function TopBar({
  recordCount,
  healthStatus,
  onRetryHealth,
  onNewChat,
  onToggleMobileMap,
  onReport,
}: TopBarProps) {
  const liveLabel =
    recordCount === undefined
      ? "Live data · loading…"
      : recordCount === null
        ? "Live data · connected"
        : `Live data · ${recordCount} records`;

  return (
    <header id="topbar">
      <div id="topbar-left">
        <img id="topbar-logo" src={logo} alt="Engage Estero" />
        <div id="topbar-divider" />
        <div id="topbar-title">
          <h1>Ask Engage Estero</h1>
          <p>Planning &amp; Zoning Decisions · Village of Estero</p>
        </div>
      </div>
      <div id="topbar-right">
        <button type="button" className="topbar-btn" onClick={onNewChat}>
          + New chat
        </button>
        <button type="button" className="topbar-btn" onClick={onReport} title="Report incorrect data">
          Report
        </button>
        <a className="topbar-btn" href={`${API_BASE}/admin.html`} title="Administrator console">
          Admin
        </a>
        <div className={`health-badge health-${healthStatus}`} role="status">
          <div className="health-dot" />
          <span>{HEALTH_LABEL[healthStatus]}</span>
          {healthStatus === "offline" && (
            <button type="button" className="retry-btn" onClick={onRetryHealth}>
              Retry
            </button>
          )}
        </div>
        <div id="live-badge">
          <div id="live-dot" />
          <span id="record-count">{liveLabel}</span>
        </div>
        <button type="button" id="mobile-map-toggle" className="map-btn" onClick={onToggleMobileMap}>
          🗺 Map
        </button>
      </div>
    </header>
  );
}
