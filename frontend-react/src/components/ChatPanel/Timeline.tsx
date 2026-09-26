import type { TimelineEntry } from "../../types";
import { statusClass, statusEmoji } from "../../lib/parseAnswer";
import { scrollToRecord } from "../../lib/recordScroll";

export default function Timeline({ entries }: { entries: TimelineEntry[] }) {
  if (entries.length === 0) return null;

  return (
    <ol className="answer-timeline">
      {entries.map((entry, i) => (
        <li key={i} className="answer-timeline-item">
          <span className={`answer-timeline-dot ${statusClass(entry.status)}`} aria-hidden="true" />
          <div className="answer-timeline-body">
            <div className="answer-timeline-head">
              {entry.date && <span className="answer-timeline-date">{entry.date}</span>}
              <span className={`proj-status ${statusClass(entry.status)}`}>
                {statusEmoji(entry.status)} {entry.status}
              </span>
            </div>
            <p className="answer-timeline-event">
              {entry.event}
              {entry.recordId && (
                <>
                  {" "}
                  <button
                    type="button"
                    className="record-id-link"
                    onClick={() => scrollToRecord(entry.recordId)}
                  >
                    [{entry.recordId}]
                  </button>
                </>
              )}
            </p>
          </div>
        </li>
      ))}
    </ol>
  );
}
