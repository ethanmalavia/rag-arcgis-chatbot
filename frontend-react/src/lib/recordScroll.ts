/** Record cards are given id={recordDomId(id)} — clicking a record-ID
 * citation anywhere (timeline, related list, inline in prose) scrolls to
 * and briefly highlights the matching card. No-ops quietly if the card
 * isn't currently rendered (e.g. it was filtered out of the visible cards). */
export function recordDomId(recordId: string): string {
  return `record-${encodeURIComponent(recordId)}`;
}

export function scrollToRecord(recordId: string): void {
  if (!recordId) return;
  const el = document.getElementById(recordDomId(recordId));
  if (!el) return;
  el.scrollIntoView({ behavior: "smooth", block: "center" });
  el.classList.add("record-flash");
  window.setTimeout(() => el.classList.remove("record-flash"), 1200);
}

/** Turn `[RECORD_ID]` citations in prose into markdown links to `#record-ID`
 * — but only for IDs the backend actually cited (recordIds), so ordinary
 * bracketed text in an answer never gets accidentally linkified. Pass the
 * resulting markdown through ReactMarkdown with a custom `a` renderer that
 * intercepts `#record-` hrefs and calls scrollToRecord instead of
 * navigating (see Message.tsx). */
export function linkifyRecordCitations(text: string, recordIds: string[]): string {
  if (!text || recordIds.length === 0) return text;
  const escaped = recordIds
    .filter(Boolean)
    .sort((a, b) => b.length - a.length) // longest-first so no partial-match shadowing
    .map((id) => id.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  if (escaped.length === 0) return text;
  const re = new RegExp(`\\[(${escaped.join("|")})\\]`, "g");
  return text.replace(re, (match, id) => `[${match}](${recordAnchorHref(id)})`);
}

export function recordAnchorHref(recordId: string): string {
  return `#${recordDomId(recordId)}`;
}
