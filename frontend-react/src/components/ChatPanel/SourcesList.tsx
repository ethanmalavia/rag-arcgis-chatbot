export default function SourcesList({ sources }: { sources: string[] }) {
  if (!sources || sources.length === 0) return null;

  return (
    <details className="sources-list">
      <summary>Sources ({sources.length})</summary>
      <ul>
        {sources.map((s, i) => (
          <li key={i}>{s}</li>
        ))}
      </ul>
    </details>
  );
}
