import type { Meeting } from "../../types";
import NextMeetings from "./NextMeetings";
import LatestNews from "./LatestNews";
import RecentDecisions from "./RecentDecisions";

interface DashboardProps {
  meetings: Meeting[];
  meetingsLoading: boolean;
  meetingsError: string | null;
  onSend: (text: string) => void;
}

export default function Dashboard({ meetings, meetingsLoading, meetingsError, onSend }: DashboardProps) {
  return (
    <div id="pulse-dashboard">
      <NextMeetings meetings={meetings} loading={meetingsLoading} error={meetingsError} />
      <LatestNews />
      <RecentDecisions onSend={onSend} />
    </div>
  );
}
