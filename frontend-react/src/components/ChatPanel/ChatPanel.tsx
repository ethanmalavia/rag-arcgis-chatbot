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
}

export default function ChatPanel({ messages, onSend, disabled, onReport }: ChatPanelProps) {
  const started = messages.length > 0;

  return (
    <section id="chat-panel">
      {started ? <MessageList messages={messages} onReport={onReport} /> : <Hero onChipClick={onSend} />}
      <ChatInput onSend={onSend} disabled={disabled} />
    </section>
  );
}
