import { useCallback, useRef, useState } from "react";
import { API_BASE } from "../lib/config";
import {
  cleanProse,
  extractJsonCards,
  liveProse,
  normalizeProject,
  parseBotText,
  parseStructuredResponse,
} from "../lib/parseAnswer";
import type { ChatApiResponse, ChatMessage, NormalizedCard, StreamDonePayload } from "../types";

function makeId(): string {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

type Setter = React.Dispatch<React.SetStateAction<ChatMessage[]>>;

type StreamEvent = { type: "token"; text?: string } | StreamDonePayload | { type: "error"; detail?: string };

function patchMessage(setMessages: Setter, id: string, patch: Partial<ChatMessage>) {
  setMessages((msgs) => msgs.map((m) => (m.id === id ? { ...m, ...patch } : m)));
}

async function tryStreamChat(question: string, botId: string, setMessages: Setter): Promise<boolean> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}/chat/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });
  } catch {
    return false;
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
  });
  return true;
}

async function plainChat(question: string, botId: string, setMessages: Setter): Promise<void> {
  try {
    const res = await fetch(`${API_BASE}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}) as { detail?: string });
      patchMessage(setMessages, botId, {
        prose: `⚠️ Backend error ${res.status}: ${err.detail || "Unknown error"}`,
        streaming: false,
        error: true,
      });
      return;
    }

    const data: ChatApiResponse = await res.json();

    if (data.projects || data.articles || data.summary) {
      const { prose, cards } = parseStructuredResponse(data);
      patchMessage(setMessages, botId, { prose, cards, sources: data.sources || [], streaming: false });
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
    patchMessage(setMessages, botId, { prose, cards, sources: data.sources || [], streaming: false });
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
  }, []);

  const send = useCallback(async (text: string) => {
    const q = text.trim();
    if (!q || sendingRef.current) return;
    sendingRef.current = true;

    const userMsg: ChatMessage = { id: makeId(), role: "user", text: q, timestamp: Date.now() };
    const botId = makeId();
    const botMsg: ChatMessage = { id: botId, role: "bot", timestamp: Date.now(), streaming: true, prose: "" };
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
