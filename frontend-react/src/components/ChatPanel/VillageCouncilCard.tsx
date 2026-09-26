import type { NormalizedCard } from "../../types";
import { statusClass, statusEmoji } from "../../lib/parseAnswer";
import { openDirections, panToCoords } from "../../lib/mapViewStore";
import { switchToTab } from "../../lib/uiStore";
import { recordDomId } from "../../lib/recordScroll";

export default function VillageCouncilCard({ card }: { card: NormalizedCard }) {
  const meta = [card.id, card.location].filter(Boolean).join(" · ");
  const minutesUrl = card.pdfUrl || card.documentUrl;
  const hasCoords = card.lat !== null && card.lng !== null;

  const showOnMap = () => {
    if (card.lat === null || card.lng === null) return;
    switchToTab("map");
    // Give the map tab a frame to become visible/sized before panning.
    requestAnimationFrame(() => panToCoords(card.lat!, card.lng!));
  };

  return (
    <div className="proj-card village-card" id={card.id ? recordDomId(card.id) : undefined}>
      <div className="card-tag card-tag-village">🏘️ Village Council</div>
      <div className="proj-title">{card.title || card.id || "Agenda Item"}</div>
      {meta && <div className="proj-meta">{meta}</div>}
      {card.summary && <div className="proj-body">{card.summary}</div>}
      {card.status ? (
        <div className={`proj-status ${statusClass(card.status)}`}>
          {statusEmoji(card.status)} {card.status}
          {card.date ? ` · ${card.date}` : ""}
        </div>
      ) : card.date ? (
        <div className="proj-meta">📅 {card.date}</div>
      ) : null}
      <div className="proj-actions">
        {minutesUrl && (
          <a
            className="btn-minutes btn-minutes-village"
            href={minutesUrl}
            target="_blank"
            rel="noopener noreferrer"
            title={card.pdfName || undefined}
          >
            📄 View Minutes
          </a>
        )}
        {card.location && (
          <button type="button" className="btn-dir" onClick={() => openDirections(card.location)}>
            📍 Directions
          </button>
        )}
        {hasCoords && (
          <button type="button" className="btn-showmap" onClick={showOnMap}>
            🧭 Show on map
          </button>
        )}
      </div>
    </div>
  );
}
