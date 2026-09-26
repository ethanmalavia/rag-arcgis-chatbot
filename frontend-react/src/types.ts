export type SourceType = "board_record" | "website_article" | "village_council";

export interface NormalizedCard {
  sourceType: SourceType;
  title: string;
  location: string;
  summary: string;
  id: string;
  status: string;
  date: string;
  documentUrl: string;
  pdfUrl: string;
  pdfName: string;
  articleUrl: string;
  publishDate: string;
  category: string;
  lat: number | null;
  lng: number | null;
}

/** One entry in a bot answer's timeline (backend/models.py TimelineEntry). */
export interface TimelineEntry {
  date: string;
  event: string;
  status: string;
  recordId: string;
}

/** A loosely-related record (same street, different project, etc.) shown
 * collapsed, one line each — backend/models.py RelatedRecord. */
export interface RelatedRecord {
  recordId: string;
  oneLine: string;
}

/** "records" = answered from Village/EsteroToday records, "general" = no
 * relevant records, answered from general knowledge, "mixed" = both. */
export type AnswerSourceType = "records" | "mixed" | "general";

export interface ChatMessage {
  id: string;
  role: "user" | "bot";
  timestamp: number;
  // user messages
  text?: string;
  // bot messages
  prose?: string;
  cards?: NormalizedCard[];
  sources?: string[];
  streaming?: boolean;
  error?: boolean;
  /** Original user question — used when posting thumbs feedback. */
  question?: string;
  route?: string;
  feedbackMeta?: Record<string, unknown>;
  feedbackRating?: "up" | "down";
  timeline?: TimelineEntry[];
  related?: RelatedRecord[];
  usedRecordIds?: string[];
  followUps?: string[];
  sourceType?: AnswerSourceType;
}

export interface ChatApiResponse {
  answer?: string;
  response?: string;
  summary?: string;
  projects?: unknown[];
  articles?: unknown[];
  sources?: string[];
  route?: string;
  meta?: Record<string, unknown>;
  timeline?: unknown[];
  related?: unknown[];
  used_record_ids?: string[];
  follow_ups?: string[];
  source_type?: AnswerSourceType;
}

export interface StreamDonePayload {
  type: "done";
  summary?: string;
  projects?: unknown[];
  articles?: unknown[];
  sources?: string[];
  route?: string;
  meta?: Record<string, unknown>;
  timeline?: unknown[];
  related?: unknown[];
  used_record_ids?: string[];
  follow_ups?: string[];
  source_type?: AnswerSourceType;
}

// ─────────────────────────────────────────────
// Community Pulse dashboard
// ─────────────────────────────────────────────
export type RightTab = "map" | "pulse";

/** One row of public/meetings.json — manually maintained, see that file's header comment. */
export interface Meeting {
  id: string;
  board: string;
  date: string; // YYYY-MM-DD
  time: string; // e.g. "5:30 PM"
  venue: string;
  /** Village minutes index the pipeline scrapes (estero-fl.gov). */
  url?: string;
}

export interface NewsPost {
  id: number;
  title: string;
  link: string;
  date: string; // ISO datetime from WordPress
}

export interface RecentDecision {
  title: string;
  date: string | null;
  board: string | null;
  status: string | null;
  applicationId: string | null;
}

// Named CalendarEvent (not Event) to avoid shadowing the DOM's global Event type.
export type EventCategory =
  | "government"
  | "music"
  | "market"
  | "sports"
  | "fair"
  | "community"
  | "other"
  /** @deprecated kept for older cached payloads */
  | "engage-estero"
  | "village";

export type EventSource = "esterotoday" | "meeting" | "venue" | "manual" | "lee_county";

export interface CalendarEvent {
  id: number | string;
  title: string;
  start: string; // local wall-clock datetime, e.g. "2026-08-18T16:30:00" (no offset)
  end: string;
  allDay: boolean;
  venue: string | null;
  url: string;
  category: EventCategory;
  source?: EventSource;
  location?: string;
}

/** Unified view of a single day's activity, merging /api/events (community
 * calendar feeds) with public/meetings.json (the hand-maintained Village
 * Council / PZDB schedule NextMeetings reads) — the mini calendar in
 * UpcomingEvents renders dots/circles and the hover/click list from this, not
 * from either source alone, so it never disagrees with Next Meetings. */
export interface DayEvent {
  source: EventSource;
  id: string; // unique across both sources
  dateKey: string; // YYYY-MM-DD
  sortKey: string; // chronologically sortable string, for ordering same-day items
  title: string;
  time: string; // display string, e.g. "4:30 PM" or "All day"
  venue: string | null;
  url: string | null; // event page, or Village minutes index for meetings
  category: EventCategory;
}

export const EVENT_CHIP_ORDER: EventCategory[] = [
  "government",
  "music",
  "market",
  "sports",
  "fair",
  "community",
  "other",
];

export const EVENT_CHIP_LABELS: Record<string, string> = {
  government: "Government",
  music: "Music",
  market: "Markets",
  sports: "Sports",
  fair: "Fairs",
  community: "Community",
  other: "Other",
  village: "Government",
  "engage-estero": "Community",
};

/** Normalize legacy village/engage-estero categories from older API payloads. */
export function normalizeEventCategory(category: string | undefined | null): EventCategory {
  if (category === "village") return "government";
  if (category === "engage-estero") return "community";
  if (category && EVENT_CHIP_ORDER.includes(category as EventCategory)) {
    return category as EventCategory;
  }
  return "other";
}
