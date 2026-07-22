import type { NormalizedCard, SourceType } from "../types";

// ─────────────────────────────────────────────
// Null-safety helpers
// ─────────────────────────────────────────────
function nullsafe(v: unknown): string {
  if (v === null || v === undefined) return "";
  const s = String(v).trim();
  return s.toLowerCase() === "null" || s.toLowerCase() === "none" || s === "" ? "" : s;
}

function cleanUrl(u: unknown): string {
  const s = nullsafe(u);
  return s.startsWith("http") ? s : "";
}

function nullableFloat(v: unknown): number | null {
  if (v === null || v === undefined || v === "") return null;
  const n = typeof v === "number" ? v : parseFloat(String(v));
  return Number.isFinite(n) ? n : null;
}

// ─────────────────────────────────────────────
// Normalization: accepts board records AND articles
// ─────────────────────────────────────────────
export function normalizeProject(p: unknown): NormalizedCard | null {
  if (!p || typeof p !== "object") return null;
  const r = p as Record<string, unknown>;

  const articleUrl = cleanUrl(r.article_url ?? r.articleUrl ?? r.url);
  const sourceType: SourceType =
    (r.source_type as SourceType) ||
    (r.sourceType as SourceType) ||
    (articleUrl ? "website_article" : "board_record");

  // The model occasionally wraps the title in markdown bold (**Title**) even
  // though card titles render as plain text, not markdown — strip it here.
  const rawTitle = String(r.title ?? r.article_title ?? r.articleTitle ?? r.project_name ?? "");

  const norm: NormalizedCard = {
    sourceType,
    title: rawTitle.replace(/\*\*/g, "").trim(),
    location: nullsafe(r.location),
    summary: nullsafe(r.summary),
    id: nullsafe(r.id) || nullsafe(r.application_id),
    status: nullsafe(r.status),
    date: nullsafe(r.date) || nullsafe(r.meeting_date),
    documentUrl: cleanUrl(r.document_url ?? r.documentUrl),
    pdfUrl: cleanUrl(r.pdf_url ?? r.pdfUrl),
    pdfName: nullsafe(r.pdf_name ?? r.pdfName),
    articleUrl,
    publishDate: nullsafe(r.publish_date) || nullsafe(r.publishDate),
    category: nullsafe(r.category),
    lat: nullableFloat(r.lat),
    lng: nullableFloat(r.lng),
  };

  if (!norm.title && !norm.id && !norm.articleUrl && !norm.documentUrl && !norm.pdfUrl) return null;
  return norm;
}

// ─────────────────────────────────────────────
// JSON block extraction (primary card path)
// ─────────────────────────────────────────────
export function extractJsonCards(text: string): NormalizedCard[] {
  const cards: NormalizedCard[] = [];
  const regex = /```json\s*([\s\S]*?)```/gi;
  let m: RegExpExecArray | null;
  while ((m = regex.exec(text)) !== null) {
    try {
      const parsed = JSON.parse(m[1].trim());
      if (Array.isArray(parsed)) {
        parsed.forEach((p) => {
          const n = normalizeProject(p);
          if (n) cards.push(n);
        });
      } else {
        const n = normalizeProject(parsed);
        if (n) cards.push(n);
      }
    } catch {
      // malformed JSON — skip
    }
  }
  return cards;
}

// Remove JSON blocks + dangling lead-in sentences from prose
export function cleanProse(text: string): string {
  return text
    .replace(/```json[\s\S]*?```/gi, "")
    .replace(/```[\s\S]*?```/g, "")
    // dangling lead-ins the model tends to write before a JSON block
    .replace(/^.*(here'?s?|the) (is )?(the )?most relevant (item|project|json block|record)( is)?[:.]?\s*$/gim, "")
    .replace(/^.*json (block|output|details?)[:.]?\s*$/gim, "")
    .replace(/^\s*relevant details?[:.]?\s*$/gim, "")
    // collapse 3+ newlines
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

// ─────────────────────────────────────────────
// Legacy delimiter parser (fallback)
// ─────────────────────────────────────────────
export function parseProjects(text: string): { projects: NormalizedCard[]; prose: string } {
  const projects: NormalizedCard[] = [];
  const patterns = [
    /START_PROJECT([\s\S]*?)END_PROJECT/g,
    /===PROJECT===([\s\S]*?)===END===/g,
    /---PROJECT---([\s\S]*?)---END---/g,
    /START_ARTICLE([\s\S]*?)END_ARTICLE/g,
  ];

  let matched = false;
  for (const regex of patterns) {
    let match: RegExpExecArray | null;
    regex.lastIndex = 0;
    while ((match = regex.exec(text)) !== null) {
      matched = true;
      const block = match[1];
      const get = (key: string) => {
        const m2 = block.match(new RegExp(key + ":\\s*(.+)"));
        return m2 ? m2[1].trim() : "";
      };
      const p = normalizeProject({
        title: get("Title"),
        id: get("ID"),
        location: get("Location"),
        summary: get("Summary"),
        status: get("Status"),
        date: get("Date"),
        document_url: get("DocumentURL"),
        article_url: get("ArticleURL"),
        publish_date: get("PublishDate"),
        category: get("Category"),
        source_type: get("SourceType"),
      });
      if (p) projects.push(p);
    }
    if (matched) break;
  }

  let prose = text;
  for (const regex of patterns) {
    regex.lastIndex = 0;
    prose = prose.replace(regex, "");
  }
  if (matched) {
    prose = prose
      .replace(/(Title|ID|Location|Summary|Status|Date|DocumentURL|ArticleURL|PublishDate|Category|SourceType):.*\n?/g, "")
      .replace(/(START|END)_(PROJECT|ARTICLE).*\n?/g, "");
  }

  return { projects, prose: cleanProse(prose) };
}

// ─────────────────────────────────────────────
// Status helpers
// ─────────────────────────────────────────────
export function statusClass(s: string): string {
  const v = (s || "").toLowerCase();
  if (v.includes("approved") || v.includes("accepted")) return "status-approved";
  if (v.includes("denied")) return "status-denied";
  if (v.includes("continued") || v.includes("recommended")) return "status-continued";
  return "status-unknown";
}

export function statusEmoji(s: string): string {
  const v = (s || "").toLowerCase();
  if (v.includes("approved") || v.includes("accepted")) return "✅";
  if (v.includes("denied")) return "❌";
  if (v.includes("continued")) return "⏳";
  if (v.includes("recommended")) return "🔁";
  return "⚪";
}

export function isArticle(p: NormalizedCard): boolean {
  return p.sourceType === "website_article" || (!!p.articleUrl && !p.documentUrl);
}

// First token of a semicolon/slash-delimited category field.
export function firstCategory(category: string): string {
  if (!category) return "";
  return category.split(/[;/]/)[0].trim();
}

// ─────────────────────────────────────────────
// Top-level: given raw bot text, produce cards + cleaned prose,
// trying JSON-block extraction first then legacy delimiters.
// ─────────────────────────────────────────────
export function parseBotText(raw: string): { prose: string; cards: NormalizedCard[] } {
  const jsonCards = extractJsonCards(raw);
  if (jsonCards.length > 0) {
    return { prose: cleanProse(raw), cards: jsonCards };
  }
  const { projects, prose } = parseProjects(raw);
  return { prose: prose || cleanProse(raw), cards: projects };
}

// While tokens are still streaming in, the model's trailing ```json fence
// arrives one token at a time and would otherwise flash as raw text mid-render.
// Cut the display text at the first fence marker (closed or not) so only
// finished prose is shown; cards are attached separately once the stream ends.
export function liveProse(text: string): string {
  const idx = text.search(/```json/i);
  const trimmed = idx === -1 ? text : text.slice(0, idx);
  return cleanProse(trimmed);
}

// Given a structured {projects, articles, summary} response, normalize into cards + prose.
export function parseStructuredResponse(data: {
  projects?: unknown[];
  articles?: unknown[];
  summary?: string;
  answer?: string;
}): { prose: string; cards: NormalizedCard[] } {
  const cards = ((data.projects || []) as unknown[])
    .concat((data.articles || []) as unknown[])
    .map(normalizeProject)
    .filter((c): c is NormalizedCard => !!c);
  const prose = cleanProse((data.summary || data.answer || "").trim());
  return { prose, cards };
}
