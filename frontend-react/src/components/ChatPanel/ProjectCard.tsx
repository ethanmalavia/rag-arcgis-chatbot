import type { NormalizedCard } from "../../types";
import { statusClass, statusEmoji } from "../../lib/parseAnswer";
import { openDirections, panToCoords } from "../../lib/mapViewStore";
import { switchToTab } from "../../lib/uiStore";
import type { ReportPrefill } from "../ReportDialog/ReportDialog";

interface ProjectCardProps {
  card: NormalizedCard;
  onReport?: (prefill: ReportPrefill) => void;
}

export default function ProjectCard({ card, onReport }: ProjectCardProps) {
  const meta = [card.id, card.location].filter(Boolean).join(" · ");
  const minutesUrl = card.pdfUrl || card.documentUrl;
  const hasCoords = card.lat !== null && card.lng !== null;

  const showOnMap = () => {
    if (card.lat === null || card.lng === null) return;
    switchToTab("map");
    requestAnimationFrame(() => panToCoords(card.lat!, card.lng!));
  };

  return (
    <div className="proj-card">
      <div className="card-tag card-tag-board">🏛 Board Record</div>
      <div className="proj-title">{card.title || card.id || "Project"}</div>
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
            className="btn-minutes"
            href={minutesUrl}
            target="_blank"
            rel="noopener noreferrer"
            title={card.pdfName || undefined}
          >
            {card.pdfUrl ? "📄 Minutes (PDF)" : "📄 View Minutes"}
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
        {onReport && (
          <button
            type="button"
            className="btn-report"
            onClick={() =>
              onReport({
                kind: card.location ? "incorrect_location" : "suggest_change",
                application_id: card.id || "",
                location: card.location || "",
                current_value: card.location || card.title || "",
              })
            }
          >
            ⚑ Report
          </button>
        )}
      </div>
    </div>
  );
}
