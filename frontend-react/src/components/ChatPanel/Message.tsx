import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { ChatMessage } from "../../types";
import { isArticle } from "../../lib/parseAnswer";
import { linkifyRecordCitations, scrollToRecord } from "../../lib/recordScroll";
import ProjectCard from "./ProjectCard";
import ArticleCard from "./ArticleCard";
import VillageCouncilCard from "./VillageCouncilCard";
import SourcesList from "./SourcesList";
import TypingIndicator from "./TypingIndicator";
import FeedbackBar from "./FeedbackBar";
import Timeline from "./Timeline";
import RelatedRecords from "./RelatedRecords";
import FollowUpChips from "./FollowUpChips";
import type { ReportPrefill } from "../ReportDialog/ReportDialog";

function formatTime(ts: number): string {
  return new Date(ts).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

/** The system prompt always opens answer_markdown with a "**Bottom line:**"
 * paragraph — split it off so it can render in its own highlighted box,
 * separate from the rest of the answer. Falls back to no special box for
 * anything that doesn't follow that convention (e.g. the plain-text JSON
 * -parse-failure fallback in rag_path.generate_answer). */
function splitBottomLine(markdown: string): { bottomLine: string | null; rest: string } {
  const breakIdx = markdown.indexOf("\n\n");
  const firstPara = (breakIdx === -1 ? markdown : markdown.slice(0, breakIdx)).trim();
  if (!/^\*\*Bottom line:?\*\*/i.test(firstPara)) {
    return { bottomLine: null, rest: markdown };
  }
  return { bottomLine: firstPara, rest: breakIdx === -1 ? "" : markdown.slice(breakIdx + 2) };
}

/** react-markdown (no rehype-raw plugin) never interprets embedded HTML —
 * it parses markdown into an AST and renders each node as a real React
 * element, so it can't be tricked into executing a <script>/onerror=...
 * payload the way dangerouslySetInnerHTML could. That's the sanitization
 * boundary here; do not add rehype-raw without also adding rehype-sanitize,
 * or this guarantee goes away. */
function Markdown({ text, recordIds }: { text: string; recordIds: string[] }) {
  const linked = linkifyRecordCitations(text, recordIds);
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={{
        a: ({ href, children, ...props }) => {
          if (href?.startsWith("#record-")) {
            return (
              <button
                type="button"
                className="record-id-link"
                onClick={() => scrollToRecord(decodeURIComponent(href.replace("#record-", "")))}
              >
                {children}
              </button>
            );
          }
          return (
            <a href={href} target="_blank" rel="noopener noreferrer" {...props}>
              {children}
            </a>
          );
        },
      }}
    >
      {linked}
    </ReactMarkdown>
  );
}

export default function Message({
  message,
  onReport,
  onSend,
}: {
  message: ChatMessage;
  onReport?: (prefill: ReportPrefill) => void;
  onSend?: (text: string) => void;
}) {
  const [copied, setCopied] = useState(false);

  if (message.role === "user") {
    return (
      <div className="msg-row">
        <div className="msg-user">
          <div className="bubble">{message.text}</div>
          <div className="msg-time">{formatTime(message.timestamp)}</div>
        </div>
      </div>
    );
  }

  const cards = message.cards || [];
  const timeline = message.timeline || [];
  const related = message.related || [];
  const followUps = message.followUps || [];
  const usedRecordIds = message.usedRecordIds || [];
  const sourceType = message.sourceType || "records";
  const showTyping = !!message.streaming && !message.prose && cards.length === 0;
  const hasContent = !!message.prose || cards.length > 0;
  const finished = !message.streaming;
  const { bottomLine, rest } = message.prose ? splitBottomLine(message.prose) : { bottomLine: null, rest: "" };

  const copyAnswer = async () => {
    if (!message.prose) return;
    try {
      await navigator.clipboard.writeText(message.prose);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // clipboard unavailable — silently ignore
    }
  };

  return (
    <div className="msg-row">
      <div className="msg-bot">
        <div className="bot-avatar">🏛</div>
        <div className="bubble">
          {showTyping && <TypingIndicator />}
          {finished && sourceType === "general" && (
            <div className="answer-source-badge answer-source-badge-general">
              General info, not from Village records
            </div>
          )}
          {/* Render order: answer (bottom line highlighted) -> timeline ->
              record cards -> related (collapsed) -> follow-up chips ->
              sources/feedback/actions. */}
          {message.prose && (
            <div className="prose prose-reveal">
              {bottomLine && (
                <div className="answer-bottom-line">
                  <Markdown text={bottomLine} recordIds={usedRecordIds} />
                </div>
              )}
              {rest && <Markdown text={rest} recordIds={usedRecordIds} />}
              {!bottomLine && !rest && <Markdown text={message.prose} recordIds={usedRecordIds} />}
            </div>
          )}
          {finished && sourceType === "mixed" && (cards.length > 0 || timeline.length > 0) && (
            <div className="answer-context-divider">General context</div>
          )}
          {finished && <Timeline entries={timeline} />}
          {/* Cards are never rendered without a summary above them — both
              are only reachable once message.prose is set (see hasContent).
              Only cards for used_record_ids reach here (see useChat.ts /
              backend rag_path.build_cards) — anything retrieved but not
              actually used by the answer never becomes a card. */}
          {message.prose &&
            cards.map((c, i) =>
              isArticle(c) ? (
                <ArticleCard key={i} card={c} />
              ) : c.sourceType === "village_council" ? (
                <VillageCouncilCard key={i} card={c} />
              ) : (
                <ProjectCard key={i} card={c} onReport={onReport} />
              ),
            )}
          {finished && <RelatedRecords items={related} />}
          {finished && onSend && <FollowUpChips questions={followUps} onSend={onSend} />}
          {finished && !hasContent && !message.error && <div>Sorry, I couldn't find an answer.</div>}
          {finished && <SourcesList sources={message.sources || []} />}
          {finished && !message.error && <FeedbackBar message={message} />}
          {finished && message.prose && (
            <div className="msg-actions">
              <button type="button" className="btn-copy" onClick={copyAnswer}>
                {copied ? "✓ Copied" : "⧉ Copy"}
              </button>
              <span className="msg-time">{formatTime(message.timestamp)}</span>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
