import { useEffect, useState } from "react";
import logo from "../../assets/logo.png";

const ADMIN_TOKEN_KEY = "ee_admin_token";

function hasAdminToken(): boolean {
  try {
    return !!localStorage.getItem(ADMIN_TOKEN_KEY);
  } catch {
    return false;
  }
}

interface TopBarProps {
  recordCount: number | null | undefined;
  onToggleMobileMap: () => void;
}

export default function TopBar({ recordCount, onToggleMobileMap }: TopBarProps) {
  const [isAdmin, setIsAdmin] = useState(hasAdminToken);

  useEffect(() => {
    // admin.html runs in its own tab/bundle and writes this key on login —
    // the storage event only fires in *other* tabs, which is exactly the
    // case we want (this chat tab picking up a login that just happened
    // elsewhere without needing a reload).
    const onStorage = (e: StorageEvent) => {
      if (e.key === ADMIN_TOKEN_KEY || e.key === null) setIsAdmin(hasAdminToken());
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

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
        {isAdmin && (
          <a
            className="topbar-btn"
            href={`${import.meta.env.BASE_URL}admin.html`}
            title="Administrator console"
          >
            Admin
          </a>
        )}
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
