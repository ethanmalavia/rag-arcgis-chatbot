import type MapView from "@arcgis/core/views/MapView";

// Small singleton so non-map components (e.g. the "Directions" button on a
// chat card) can reach the live MapView instance without prop-drilling it
// through the whole tree. Set by MapPanel on mount, cleared on unmount.
let currentView: MapView | null = null;

export function setMapView(view: MapView | null) {
  currentView = view;
}

export function getMapView(): MapView | null {
  return currentView;
}

export async function panToAddress(address: string): Promise<void> {
  const view = currentView;
  if (!view) return;
  try {
    const locator = await import("@arcgis/core/rest/locator.js");
    const result = await locator.addressToLocations(
      "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer",
      { address: { SingleLine: address }, maxLocations: 1 },
    );
    const location = result[0]?.location;
    if (location) {
      view.goTo({ target: location, zoom: 15 });
    }
  } catch {
    // best-effort only; the Google Maps tab is the primary directions path
  }
}

export function openDirections(address: string): void {
  const query = encodeURIComponent(`${address}, Estero, FL`);
  window.open(`https://www.google.com/maps/search/?api=1&query=${query}`, "_blank", "noopener");
  void panToAddress(address);
}

/** Pan/zoom straight to known coordinates — no geocoding round-trip needed.
 * Used by the chat card "Show on map" link, which already has lat/lng from
 * the board record's own metadata. */
export function panToCoords(lat: number, lng: number, zoom = 16): void {
  const view = currentView;
  if (!view) return;
  view.goTo({ center: [lng, lat], zoom }).catch(() => {
    // view may still be mid-resize right after a tab switch — best-effort only
  });
}

interface ViewState {
  center: [number, number];
  zoom: number;
}

let savedViewState: ViewState | null = null;

/** The MapView's container goes through display:none <-> display:flex when
 * the Map/Pulse tab switches (kept mounted rather than unmounted so the
 * expensive WebGL view survives). Toggling a WebGL canvas's container to/from
 * zero size isn't something ArcGIS's own resize handling reliably preserves
 * center/zoom across, so RightPanel calls these explicitly around every
 * switch as a belt-and-suspenders fix. */
export function captureViewState(): void {
  const view = currentView;
  const { longitude, latitude } = view?.center ?? {};
  if (longitude == null || latitude == null) return;
  savedViewState = { center: [longitude, latitude], zoom: view!.zoom };
}

export function restoreViewState(): void {
  const view = currentView;
  if (!view || !savedViewState) return;
  const { center, zoom } = savedViewState;
  view.goTo({ center, zoom }, { animate: false }).catch(() => {
    // best-effort — if this races with something else, worst case the pan is off by a frame
  });
}
