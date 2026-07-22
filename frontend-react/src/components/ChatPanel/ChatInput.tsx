import { useRef, useState } from "react";
import type { KeyboardEvent } from "react";

interface ChatInputProps {
  onSend: (text: string) => void;
  disabled?: boolean;
}

export default function ChatInput({ onSend, disabled }: ChatInputProps) {
  const [value, setValue] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  const autoResize = () => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 100)}px`;
  };

  const submit = () => {
    const q = value.trim();
    if (!q || disabled) return;
    onSend(q);
    setValue("");
    requestAnimationFrame(autoResize);
  };

  const handleKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  return (
    <div id="input-bar">
      <div id="input-wrap">
        <label htmlFor="question" className="sr-only">
          Ask a question
        </label>
        <textarea
          id="question"
          ref={textareaRef}
          rows={1}
          placeholder="Ask about a project, location, year, or decision…"
          value={value}
          onChange={(e) => {
            setValue(e.target.value);
            autoResize();
          }}
          onKeyDown={handleKey}
        />
        <button type="button" id="send-btn" onClick={submit} title="Send" aria-label="Send message" disabled={disabled}>
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M2 21l21-9L2 3v7l15 2-15 2z" />
          </svg>
        </button>
      </div>
      <div id="input-footer">Data: Village of Estero Planning, Zoning &amp; Design Board · Built by Engage Estero</div>
    </div>
  );
}
