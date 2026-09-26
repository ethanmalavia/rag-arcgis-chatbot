import { useCallback, useRef, useState } from "react";
import { API_BASE } from "../lib/config";
import { apiHeaders } from "../lib/deviceId";
import {
  cleanProse,
  extractJsonCards,
  liveProse,
  normalizeProject,
  normalizeRelated,
  normalizeTimeline,
  parseBotText,
  parseStructuredResponse,
} from "../lib/parseAnswer";
import { clearResultsOnMap, showResultsOnMap } from "../lib/mapViewStore";
import { switchToTab } from "../lib/uiStore";
import type { ChatApiResponse, ChatMessage, NormalizedCard, StreamDonePayload } from "../types";

function makeId(): string {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

/** After an answer lands, if any result cards carry coordinates, reveal the map
 * and zoom/fit to them so the map tracks what the user asked about. Queries with
 * no located results (e.g. "how many approved items") leave the map untouched. */
function focusMapOnCards(cards: NormalizedCard[]): void {
  const points = cards
    .filter((c) => c.lat !== null && c.lng !== null)
    .map((c) => ({ lat: c.lat as number, lng: c.lng as number, label: c.title || c.id }));
  if (points.length === 0) return;
  switchToTab("map");
  // Double rAF: if the Map tab was hidden, this lets it become visible and
  // resize to its real dimensions before we recentre, so the target lands at
  // the true centre (same guard RightPanel uses when restoring view state).
  requestAnimationFrame(() => requestAnimationFrame(() => void showResultsOnMap(points)));
}

type Setter = React.Dispatch<React.SetStateAction<ChatMessage[]>>;

type StreamEvent =
  | { type: "token"; text?: string }
  | { type: "meta"; route?: string; llm_mode?: string; [key: string]: unknown }
  | StreamDonePayload
  | { type: "error"; detail?: string };

function patchMessage(setMessages: Setter, id: string, patch: Partial<ChatMessage>) {
  setMessages((msgs) => msgs.map((m) => (m.id === id ? { ...m, ...patch } : m)));
}

function rateLimitProse(status: number, detail?: string): string {
  if (status === 429) {
    return "⚠️ Too many requests — wait a moment and try again.";
  }
  return `⚠️ Backend error ${status}: ${detail || "Unknown error"}`;
}

async function tryStreamChat(question: string, botId: string, setMessages: Setter): Promise<boolean> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}/chat/stream`, {
      method: "POST",
      headers: apiHeaders(),
      body: JSON.stringify({ question }),
    });
  } catch {
    return false;
  }
  if (res.status === 429) {
    const err = await res.json().catch(() => ({}) as { detail?: string });
    patchMessage(setMessages, botId, {
      prose: rateLimitProse(429, err.detail),
      streaming: false,
      error: true,
    });
    return true;
  }
  if (!res.ok || !res.body) return false;

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let fullText = "";
  let donePayload: StreamDonePayload | null = null;

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split("\n\n");
    buffer = parts.pop() || "";
    for (const part of parts) {
      if (!part.startsWith("data: ")) continue;
      let payload: StreamEvent;
      try {
        payload = JSON.parse(part.slice(6));
      } catch {
        continue;
      }
      if (payload.type === "token" && payload.text) {
        fullText += payload.text;
        patchMessage(setMessages, botId, { prose: liveProse(fullText), streaming: true });
      } else if (payload.type === "done") {
        donePayload = payload;
      } else if (payload.type === "meta" && "route" in payload && payload.route) {
        patchMessage(setMessages, botId, { route: String(payload.route) });
      } else if (payload.type === "error") {
        patchMessage(setMessages, botId, {
          prose: "⚠️ " + (payload.detail || "Stream error"),
          streaming: false,
          error: true,
        });
      }
    }
  }

  const cards: NormalizedCard[] = donePayload
    ? ([] as unknown[])
        .concat(donePayload.projects || [], donePayload.articles || [])
        .map(normalizeProject)
        .filter((c): c is NormalizedCard => !!c)
    : extractJsonCards(fullText);
  const finalProse = cleanProse((donePayload && donePayload.summary) || fullText);

  patchMessage(setMessages, botId, {
    prose: finalProse,
    cards,
    sources: donePayload?.sources || [],
    streaming: false,
    route: donePayload?.route,
    feedbackMeta: donePayload?.meta,
    timeline: normalizeTimeline(donePayload?.timeline),
    related: normalizeRelated(donePayload?.related),
    usedRecordIds: (donePayload?.used_record_ids || []).filter((id): id is string => !!id),
    followUps: (donePayload?.follow_ups || []).filter((q): q is string => !!q),
    // Only badge "general" when the backend says so — a card-less answer
    // (events, council summaries) is still grounded in Village records.
    sourceType: donePayload?.source_type || "records",
  });
  focusMapOnCards(cards);
  return true;
}

async function plainChat(question: string, botId: string, setMessages: Setter): Promise<void> {
  try {
    const res = await fetch(`${API_BASE}/chat`, {
      method: "POST",
      headers: apiHeaders(),
      body: JSON.stringify({ question }),
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}) as { detail?: string });
      patchMessage(setMessages, botId, {
        prose: rateLimitProse(res.status, err.detail),
        streaming: false,
        error: true,
      });
      return;
    }

    const data: ChatApiResponse = await res.json();

    if (data.projects || data.articles || data.summary) {
      const { prose, cards, timeline, related, usedRecordIds, followUps, sourceType } =
        parseStructuredResponse(data);
      patchMessage(setMessages, botId, {
        prose,
        cards,
        sources: data.sources || [],
        streaming: false,
        route: data.route,
        feedbackMeta: data.meta,
        timeline,
        related,
        usedRecordIds,
        followUps,
        sourceType,
      });
      focusMapOnCards(cards);
      return;
    }

    const raw = data.answer || data.response || "";
    const { prose, cards } = parseBotText(raw);

    if (!prose && cards.length === 0) {
      patchMessage(setMessages, botId, {
        prose: "I received an empty response from the backend. Please try again.",
        streaming: false,
      });
      return;
    }
    patchMessage(setMessages, botId, {
      prose,
      cards,
      sources: data.sources || [],
      streaming: false,
      route: data.route,
      feedbackMeta: data.meta,
    });
    focusMapOnCards(cards);
  } catch {
    patchMessage(setMessages, botId, {
      prose: `⚠️ Could not reach the backend at ${API_BASE}. Is uvicorn running?`,
      streaming: false,
      error: true,
    });
  }
}

export function useChat() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const sendingRef = useRef(false);

  const newChat = useCallback(() => {
    setMessages([]);
    clearResultsOnMap();
  }, []);

  const send = useCallback(async (text: string) => {
    const q = text.trim();
    if (!q || sendingRef.current) return;
    sendingRef.current = true;

    const userMsg: ChatMessage = { id: makeId(), role: "user", text: q, timestamp: Date.now() };
    const botId = makeId();
    const botMsg: ChatMessage = {
      id: botId,
      role: "bot",
      timestamp: Date.now(),
      streaming: true,
      prose: "",
      question: q,
    };
    setMessages((m) => [...m, userMsg, botMsg]);

    try {
      const streamed = await tryStreamChat(q, botId, setMessages);
      if (!streamed) {
        await plainChat(q, botId, setMessages);
      }
    } finally {
      sendingRef.current = false;
    }
  }, []);

  return { messages, send, newChat };
}
