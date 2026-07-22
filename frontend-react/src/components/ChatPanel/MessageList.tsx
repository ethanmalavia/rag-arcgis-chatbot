import type { ChatMessage } from "../../types";
import { useAutoScroll } from "../../hooks/useAutoScroll";
import Message from "./Message";
import type { ReportPrefill } from "../ReportDialog/ReportDialog";

interface MessageListProps {
  messages: ChatMessage[];
  onReport?: (prefill: ReportPrefill) => void;
}

export default function MessageList({ messages, onReport }: MessageListProps) {
  const { ref, onScroll } = useAutoScroll<HTMLDivElement>([messages]);

  return (
    <div
      id="messages"
      className="visible"
      ref={ref}
      onScroll={onScroll}
      role="log"
      aria-live="polite"
      aria-relevant="additions"
      aria-label="Conversation"
    >
      {messages.map((m) => (
        <Message key={m.id} message={m} onReport={onReport} />
      ))}
    </div>
  );
}
