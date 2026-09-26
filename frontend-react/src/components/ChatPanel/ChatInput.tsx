import { useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import PromptHelpDialog from "../PromptHelp/PromptHelpDialog";

interface ChatInputProps {
  onSend: (text: string) => void;
  disabled?: boolean;
}

export default function ChatInput({ onSend, disabled }: ChatInputProps) {
  const [value, setValue] = useState("");
  const [helpOpen, setHelpOpen] = useState(false);
  const [justSent, setJustSent] = useState(false);
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
    setJustSent(true);
    window.setTimeout(() => setJustSent(false), 320);
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
        <button
          type="button"
          className="btn-prompt-help"
          onClick={() => setHelpOpen(true)}
          title="Help me ask a question"
          aria-label="Help me ask a question"
          disabled={disabled}
        >
          ?
        </button>
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
        <button
          type="button"
          id="send-btn"
          className={justSent ? "send-pulse" : ""}
          onClick={submit}
          title="Send"
          aria-label="Send message"
          disabled={disabled}
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M2 21l21-9L2 3v7l15 2-15 2z" />
          </svg>
        </button>
      </div>
      <div id="input-footer">Data: Village of Estero · Built by Engage Estero</div>
      <PromptHelpDialog
        open={helpOpen}
        onClose={() => setHelpOpen(false)}
        onPickPrompt={(prompt) => onSend(prompt)}
      />
    </div>
  );
}
