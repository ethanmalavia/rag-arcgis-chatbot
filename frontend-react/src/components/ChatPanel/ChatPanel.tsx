import { useState } from "react";
import type { ChatMessage } from "../../types";
import Hero from "./Hero";
import MessageList from "./MessageList";
import ChatInput from "./ChatInput";
import type { ReportPrefill } from "../ReportDialog/ReportDialog";

interface ChatPanelProps {
  messages: ChatMessage[];
  onSend: (text: string) => void;
  disabled: boolean;
  onReport?: (prefill: ReportPrefill) => void;
  onNewChat: () => void;
}

const CLEAR_FADE_MS = 220;

export default function ChatPanel({ messages, onSend, disabled, onReport, onNewChat }: ChatPanelProps) {
  const started = messages.length > 0;
  const [isClearing, setIsClearing] = useState(false);

  const handleNewChat = () => {
    if (isClearing || !started) return;
    setIsClearing(true);
    // Actual clear happens after the fade-out finishes, so the outgoing
    // messages are still on screen (just fading) for the duration — the
    // hero mounts fresh right after, with its own fade-in.
    window.setTimeout(() => {
      onNewChat();
      setIsClearing(false);
    }, CLEAR_FADE_MS);
  };

  return (
    <section id="chat-panel">
      {started && (
        <button type="button" className="new-chat-pill" onClick={handleNewChat} title="Start a new chat">
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M12 5v14M5 12h14" stroke="currentColor" strokeWidth="2" strokeLinecap="round" fill="none" />
          </svg>
          New chat
        </button>
      )}
      <div className={`chat-body${isClearing ? " chat-clearing" : ""}`}>
        {started ? <MessageList messages={messages} onReport={onReport} onSend={onSend} /> : <Hero />}
      </div>
      {/* DatasetBar (Load CSV) intentionally hidden — the dataset already loads
          at backend startup. Component and POST /load left in place; see
          DatasetBar.tsx to re-enable. */}
      <ChatInput onSend={onSend} disabled={disabled} />
    </section>
  );
}
