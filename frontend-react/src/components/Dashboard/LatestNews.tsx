import { useNews } from "../../hooks/useNews";
import { SkeletonRows } from "./Skeleton";

function formatDate(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
}

export default function LatestNews() {
  const { posts, loading, error, retry } = useNews();

  return (
    <section className="pulse-widget pulse-widget-gold">
      <div className="pulse-widget-header">
        <h3>Latest from EsteroToday</h3>
      </div>

      {loading && <SkeletonRows count={3} />}

      {!loading && error && (
        <p className="pulse-empty">
          Couldn't load news —{" "}
          <a href="https://esterotoday.com" target="_blank" rel="noopener noreferrer">
            visit esterotoday.com ↗
          </a>
          <button type="button" className="pulse-retry" onClick={retry}>
            Retry
          </button>
        </p>
      )}

      {!loading && !error && (
        <ul className="news-list">
          {posts.map((post) => (
            <li className="news-row" key={post.id}>
              <a className="news-link" href={post.link} target="_blank" rel="noopener noreferrer">
                <span className="news-dot" aria-hidden="true" />
                <span className="news-body">
                  <span className="news-title">{post.title}</span>
                  <span className="news-date">{formatDate(post.date)}</span>
                </span>
              </a>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
