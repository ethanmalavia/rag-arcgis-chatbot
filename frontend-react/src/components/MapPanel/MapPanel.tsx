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

// ArcGIS StoryMap "Where Estero Decides: 10 Years of Village Decisions", the
// alternate view behind the Map | Story switch. It must stay *published*, not
// just shared publicly: an unpublished story redirects anonymous visitors to
// an ArcGIS sign-in page that refuses to render in an iframe. The story sizes
// itself to the iframe, not the window, and below some width it swaps its
// inline dashboards for link cards, so App widens the panel while it's shown
// (see STORY_MIN_WIDTH_PX in App.tsx).
const STORY_MAP_ID = "185eb6332dbf45be8be5213258916b49";
const STORY_MAP_URL = `https://storymaps.arcgis.com/stories/${STORY_MAP_ID}`;

// The app's underlying feature layer is public on its own, so the record
// count in TopBar ("Live data · N records") can still be queried directly —
// no need to lose that just because the map itself moved into an iframe.
const RECORD_COUNT_URL =
  "https://services2.arcgis.com/UzlfiFv8kzq0Q4vo/arcgis/rest/services/Estero_Board_Records_%E2%80%94_All_Categories/FeatureServer/0/query?where=1%3D1&returnCountOnly=true&f=json";

export type MapPanelView = "map" | "story";

const VIEWS: Record<MapPanelView, { url: string; title: string; subtitle: string; label: string }> = {
  map: {
    url: INSTANT_APP_URL,
    title: "Project Map",
    subtitle: "Village of Estero · Live ArcGIS",
    label: "Estero Board Records",
  },
  story: {
    url: STORY_MAP_URL,
    title: "Where Estero Decides",
    subtitle: "Village of Estero · ArcGIS StoryMap",
    label: "10 Years of Village Decisions",
  },
};

interface MapPanelProps {
  expanded: boolean;
  onToggleExpand: () => void;
  onRecordCount: (count: number | null) => void;
  onViewChange: (view: MapPanelView) => void;
}

export default function MapPanel({ expanded, onToggleExpand, onRecordCount, onViewChange }: MapPanelProps) {
  const [view, setView] = useState<MapPanelView>("map");
  // The story iframe is only created on first switch (so the default Map view
  // doesn't pay for loading it), then kept mounted so switching back and forth
  // doesn't reload it or lose the reader's scroll position.
  const [storyLoaded, setStoryLoaded] = useState(false);
  const [loadError, setLoadError] = useState<Record<MapPanelView, boolean>>({ map: false, story: false });
  const active = VIEWS[view];

  const switchView = (next: MapPanelView) => {
    if (next === view) return;
    if (next === "story") setStoryLoaded(true);
    setView(next);
    onViewChange(next);
  };

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
          <div id="map-icon">{view === "map" ? "🗺" : "📖"}</div>
          <div id="map-title">
            <strong>{active.title}</strong>
            <span>{active.subtitle}</span>
          </div>
        </div>
        <div id="map-actions">
          <div className="map-view-switch" role="group" aria-label="Map or story view">
            <button
              type="button"
              className={`map-btn ${view === "map" ? "active" : ""}`}
              aria-pressed={view === "map"}
              onClick={() => switchView("map")}
            >
              Map
            </button>
            <button
              type="button"
              className={`map-btn ${view === "story" ? "active" : ""}`}
              aria-pressed={view === "story"}
              onClick={() => switchView("story")}
            >
              Story
            </button>
          </div>
          <button type="button" className="map-btn" id="expand-btn" onClick={onToggleExpand}>
            {expanded ? "⤡ Collapse" : "⤢ Expand"}
          </button>
          <a className="map-btn" href={active.url} target="_blank" rel="noopener noreferrer">
            ↗ Open
          </a>
        </div>
      </div>
      <div id="map-label">{active.label}</div>
      {loadError[view] && (
        <div id="map-error-banner" role="alert">
          ⚠️ The {view === "map" ? "map" : "story"} failed to load.{" "}
          <a href={active.url} target="_blank" rel="noopener noreferrer">
            Open it directly on ArcGIS Online
          </a>
          .
        </div>
      )}
      {/* Cross-origin iframes: onError can catch a hard network/navigation
          failure, but not an error the Esri app renders inside its own page —
          that's invisible to us. The "Open directly" link above is the real
          fallback for that case. */}
      <iframe
        id="viewDiv"
        title="Estero Board Records map"
        src={INSTANT_APP_URL}
        hidden={view !== "map"}
        onError={() => setLoadError((e) => ({ ...e, map: true }))}
        style={{ border: "none", width: "100%", height: "100%" }}
      />
      {storyLoaded && (
        <iframe
          className="map-story-frame"
          title="Where Estero Decides story map"
          src={STORY_MAP_URL}
          hidden={view !== "story"}
          // Same permissions as Esri's own StoryMap embed snippet.
          allowFullScreen
          allow="geolocation"
          onError={() => setLoadError((e) => ({ ...e, story: true }))}
          style={{ border: "none", width: "100%", height: "100%" }}
        />
      )}
      <div id="map-footer">
        <div id="map-footer-dot" />
        Powered by Esri · ArcGIS Online
      </div>
    </section>
  );
}
