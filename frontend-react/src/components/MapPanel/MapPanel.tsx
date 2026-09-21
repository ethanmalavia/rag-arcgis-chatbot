import { useEffect, useState } from "react";
import { setMapView } from "../../lib/mapViewStore";

// Esri "Nearby" Instant App wrapping webmap 84a56d2f741d49f5a70c547923fb45d5
// (same webmap this panel used to load directly via the JS SDK). Embedding
// the hosted app itself — not the raw webmap — was a deliberate choice: it
// picks up the app's own theme/search config, at the cost of native SDK
// control. See mapViewStore.ts: every pan/capture/restore call there already
// no-ops safely when there's no live MapView (setMapView is never called
// here), so "Show on map" on a chat card still switches to this tab, it just
// can't zoom to the specific coordinates inside an opaque cross-origin iframe.
const INSTANT_APP_ID = "4bd1bf488ac943d7966af9f3ccd22302";
const INSTANT_APP_URL = `https://eccl-swfl-safety.maps.arcgis.com/apps/instant/nearby/index.html?appid=${INSTANT_APP_ID}`;

// The app's underlying feature layer is public on its own, so the record
// count in TopBar ("Live data · N records") can still be queried directly —
// no need to lose that just because the map itself moved into an iframe.
const RECORD_COUNT_URL =
  "https://services2.arcgis.com/UzlfiFv8kzq0Q4vo/arcgis/rest/services/Estero_Board_Records_%E2%80%94_All_Categories/FeatureServer/0/query?where=1%3D1&returnCountOnly=true&f=json";

interface MapPanelProps {
  expanded: boolean;
  onToggleExpand: () => void;
  onRecordCount: (count: number | null) => void;
}

export default function MapPanel({ expanded, onToggleExpand, onRecordCount }: MapPanelProps) {
  const [loadError, setLoadError] = useState(false);

  useEffect(() => {
    // No live MapView to hand out from an iframe — clear any stale reference
    // so "Show on map" etc. degrade to their already-safe no-op path.
    setMapView(null);

    let cancelled = false;
    fetch(RECORD_COUNT_URL)
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json() as Promise<{ count?: number; error?: unknown }>;
      })
      .then((data) => {
        if (cancelled) return;
        if (typeof data.count === "number") onRecordCount(data.count);
        else onRecordCount(null);
      })
      .catch(() => {
        if (!cancelled) onRecordCount(null);
      });

    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <section id="map-panel">
      <div id="map-header">
        <div id="map-header-left">
          <div id="map-icon">🗺</div>
          <div id="map-title">
            <strong>Project Map</strong>
            <span>Village of Estero · Live ArcGIS</span>
          </div>
        </div>
        <div id="map-actions">
          <button type="button" className="map-btn" id="expand-btn" onClick={onToggleExpand}>
            {expanded ? "⤡ Collapse" : "⤢ Expand"}
          </button>
          <a className="map-btn" href={INSTANT_APP_URL} target="_blank" rel="noopener noreferrer">
            ↗ Open
          </a>
        </div>
      </div>
      <div id="map-label">Estero Board Records</div>
      {loadError && (
        <div id="map-error-banner" role="alert">
          ⚠️ The map failed to load.{" "}
          <a href={INSTANT_APP_URL} target="_blank" rel="noopener noreferrer">
            Open it directly on ArcGIS Online
          </a>
          .
        </div>
      )}
      <iframe
        id="viewDiv"
        title="Estero Board Records map"
        src={INSTANT_APP_URL}
        // Cross-origin iframe: this can catch a hard network/navigation
        // failure, but not an error the Esri app renders inside its own
        // page — that's invisible to us. The "Open directly" link above is
        // the real fallback for that case.
        onError={() => setLoadError(true)}
        style={{ border: "none", width: "100%", height: "100%" }}
      />
      <div id="map-footer">
        <div id="map-footer-dot" />
        Powered by Esri · ArcGIS Online
      </div>
    </section>
  );
}
