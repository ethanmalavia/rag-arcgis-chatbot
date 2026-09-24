import logo from "../../assets/logo.png";

interface TopBarProps {
  recordCount: number | null | undefined;
  onNewChat: () => void;
  onToggleMobileMap: () => void;
}

export default function TopBar({
  recordCount,
  onNewChat,
  onToggleMobileMap,
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
        <a
          className="topbar-btn"
          href={`${import.meta.env.BASE_URL}admin.html`}
          title="Administrator console"
        >
          Admin
        </a>
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
