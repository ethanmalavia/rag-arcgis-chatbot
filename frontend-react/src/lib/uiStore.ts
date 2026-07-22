import type { RightTab } from "../types";

// Small singleton (same pattern as mapViewStore.ts) so components outside the
// right-panel subtree — e.g. a "Show on map" link deep inside a chat card —
// can switch the Map/Pulse tab without threading state through two unrelated
// branches of the tree. Registered by RightPanel on mount, cleared on unmount.
let setTab: ((tab: RightTab) => void) | null = null;

export function registerTabSwitcher(fn: ((tab: RightTab) => void) | null): void {
  setTab = fn;
}

export function switchToTab(tab: RightTab): void {
  setTab?.(tab);
}
