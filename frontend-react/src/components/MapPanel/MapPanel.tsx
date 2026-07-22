import { useEffect, useRef, useState } from "react";
import WebMap from "@arcgis/core/WebMap";
import MapView from "@arcgis/core/views/MapView";
import Home from "@arcgis/core/widgets/Home";
import Zoom from "@arcgis/core/widgets/Zoom";
import Search from "@arcgis/core/widgets/Search";
import type FeatureLayer from "@arcgis/core/layers/FeatureLayer";
import "@arcgis/core/assets/esri/themes/light/main.css";
import { setMapView } from "../../lib/mapViewStore";

const PORTAL_ITEM_ID = "93eef5bd592f48b4a04e20815dba13b6";

interface MapPanelProps {
  expanded: boolean;
  onToggleExpand: () => void;
  onRecordCount: (count: number | null) => void;
}

// MapView (v5 @arcgis/core) watches its container with a ResizeObserver and
// resizes itself automatically — no manual view.resize() call needed when
// the grid-template-columns transition changes the container's width.

export default function MapPanel({ expanded, onToggleExpand, onRecordCount }: MapPanelProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const viewRef = useRef<MapView | null>(null);
  const [loadError, setLoadError] = useState(false);

  useEffect(() => {
    if (!containerRef.current) return;
    let cancelled = false;

    const webmap = new WebMap({ portalItem: { id: PORTAL_ITEM_ID } });
    const view = new MapView({ container: containerRef.current, map: webmap, ui: { components: [] } });
    view.ui.add(new Zoom({ view }), "top-left");
    view.ui.add(new Home({ view }), "top-left");
    view.ui.add(new Search({ view }), "top-right");
    viewRef.current = view;
    setMapView(view);

    webmap
      .when(() => {
        if (cancelled) return;
        setLoadError(false);
        const featureLayers = webmap.layers.toArray().filter((l) => l.type === "feature") as FeatureLayer[];
        let total = 0;
        const loads = featureLayers.map((l) =>
          l.load().then(() => l.queryFeatureCount().then((n) => { total += n; })),
        );
        Promise.all(loads)
          .then(() => { if (!cancelled) onRecordCount(total); })
          .catch(() => { if (!cancelled) onRecordCount(null); });
      })
      .catch((err: unknown) => {
        // In StrictMode dev, the first mount is deliberately torn down mid-load
        // (view.destroy() below aborts the in-flight request) — that's expected
        // and not a real failure, so only surface errors from the live mount.
        if (cancelled) return;
        console.error("Failed to load web map:", err);
        setLoadError(true);
      });

    return () => {
      cancelled = true;
      setMapView(null);
      viewRef.current = null;
      view.destroy();
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
          <a
            className="map-btn"
            href={`https://www.arcgis.com/apps/mapviewer/index.html?webmap=${PORTAL_ITEM_ID}`}
            target="_blank"
            rel="noopener noreferrer"
          >
            ↗ Open
          </a>
        </div>
      </div>
      <div id="map-label">PZDB_Pilot</div>
      {loadError && (
        <div id="map-error-banner" role="alert">
          ⚠️ The map failed to load.{" "}
          <a
            href={`https://www.arcgis.com/apps/mapviewer/index.html?webmap=${PORTAL_ITEM_ID}`}
            target="_blank"
            rel="noopener noreferrer"
          >
            Open it directly on ArcGIS Online
          </a>
          .
        </div>
      )}
      <div id="viewDiv" ref={containerRef} />
      <div id="map-footer">
        <div id="map-footer-dot" />
        Powered by Esri · ArcGIS Online
      </div>
    </section>
  );
}
