export type SourceType = "board_record" | "website_article";

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
}

export interface ChatApiResponse {
  answer?: string;
  response?: string;
  summary?: string;
  projects?: unknown[];
  articles?: unknown[];
  sources?: string[];
}

export interface StreamDonePayload {
  type: "done";
  summary?: string;
  projects?: unknown[];
  articles?: unknown[];
  sources?: string[];
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
