import type { NormalizedCard } from "../../types";
import { firstCategory } from "../../lib/parseAnswer";
import { openDirections } from "../../lib/mapViewStore";

export default function ArticleCard({ card }: { card: NormalizedCard }) {
  const category = firstCategory(card.category);

  return (
    <div className="proj-card article-card">
      <div className="card-tag card-tag-article">📰 EsteroToday Article</div>
      <div className="proj-title">{card.title || "Article"}</div>
      {(category || card.publishDate) && (
        <div className="proj-meta-row">
          {category && <span className="category-badge">{category}</span>}
          {card.publishDate && <span className="proj-meta">{card.publishDate}</span>}
        </div>
      )}
      {card.summary && <div className="proj-body proj-body-clamp">{card.summary}</div>}
      <div className="proj-actions">
        {card.articleUrl && (
          <a className="btn-article" href={card.articleUrl} target="_blank" rel="noopener noreferrer">
            📰 Read Article ↗
          </a>
        )}
        {card.location && (
          <button type="button" className="btn-dir" onClick={() => openDirections(card.location)}>
            📍 Directions
          </button>
        )}
      </div>
    </div>
  );
}
