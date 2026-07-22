import { useRecentDecisions } from "../../hooks/useRecentDecisions";
import { statusClass, statusEmoji } from "../../lib/parseAnswer";
import { SkeletonRows } from "./Skeleton";

interface RecentDecisionsProps {
  onSend: (text: string) => void;
}

/** Clicking a row asks the chat about that project — the dashboard/chat tie-in
 * that's the point of this widget. `onSend` is the same useChat().send used
 * by the chat input, threaded down from App. */
export default function RecentDecisions({ onSend }: RecentDecisionsProps) {
  const { decisions, loading, error, retry } = useRecentDecisions();

  return (
    <section className="pulse-widget pulse-widget-navy">
      <div className="pulse-widget-header">
        <h3>Recent Board Decisions</h3>
      </div>

      {loading && <SkeletonRows count={3} />}

      {!loading && error && (
        <p className="pulse-empty">
          Couldn't load recent decisions.
          <button type="button" className="pulse-retry" onClick={retry}>
            Retry
          </button>
        </p>
      )}

      {!loading && !error && decisions.length === 0 && <p className="pulse-empty">No recent decisions on file.</p>}

      {!loading && !error && decisions.length > 0 && (
        <ul className="decision-list">
          {decisions.map((d, i) => (
            <li key={i}>
              <button
                type="button"
                className="decision-row"
                onClick={() => onSend(`Tell me about ${d.title}`)}
                title={`Ask the chat about ${d.title}`}
              >
                {d.status ? (
                  <span className={`decision-pill ${statusClass(d.status)}`}>{statusEmoji(d.status)}</span>
                ) : (
                  <span className="decision-pill status-unknown">⚪</span>
                )}
                <span className="decision-info">
                  <span className="decision-title">{d.title}</span>
                  <span className="decision-meta">{[d.date, d.board].filter(Boolean).join(" · ")}</span>
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
