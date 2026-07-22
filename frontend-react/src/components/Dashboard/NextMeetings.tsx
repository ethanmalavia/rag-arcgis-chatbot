import type { Meeting } from "../../types";
import { meetingIsThisWeek } from "../../hooks/useMeetings";
import { SkeletonRows } from "./Skeleton";

interface NextMeetingsProps {
  meetings: Meeting[];
  loading: boolean;
  error: string | null;
}

function formatMonthDay(dateStr: string): { month: string; day: number } {
  const d = new Date(`${dateStr}T00:00:00`);
  return { month: d.toLocaleDateString("en-US", { month: "short" }).toUpperCase(), day: d.getDate() };
}

export default function NextMeetings({ meetings, loading, error }: NextMeetingsProps) {
  return (
    <section className="pulse-widget pulse-widget-navy">
      <div className="pulse-widget-header">
        <h3>Next Meetings</h3>
      </div>

      {loading && <SkeletonRows count={2} />}

      {!loading && error && <p className="pulse-empty">Couldn't load the meeting schedule.</p>}

      {!loading && !error && meetings.length === 0 && (
        <p className="pulse-empty">
          No upcoming meetings listed here yet — check the Village calendar directly.
        </p>
      )}

      {!loading && !error && meetings.length > 0 && (
        <ul className="meeting-list">
          {meetings.map((m) => {
            const { month, day } = formatMonthDay(m.date);
            return (
              <li className="meeting-row" key={m.id}>
                <div className="date-leaf">
                  <span className="date-leaf-month">{month}</span>
                  <span className="date-leaf-day">{day}</span>
                </div>
                <div className="meeting-info">
                  <div className="meeting-name-row">
                    <span className="meeting-name">{m.board}</span>
                    {meetingIsThisWeek(m.date) && <span className="chip-thisweek">This week</span>}
                  </div>
                  <div className="meeting-meta">
                    {m.time} · {m.venue}
                  </div>
                </div>
              </li>
            );
          })}
        </ul>
      )}

      <a
        className="pulse-widget-link"
        href="https://estero-fl.gov/agendas-minutes/"
        target="_blank"
        rel="noopener noreferrer"
      >
        Agendas ↗
      </a>
    </section>
  );
}
