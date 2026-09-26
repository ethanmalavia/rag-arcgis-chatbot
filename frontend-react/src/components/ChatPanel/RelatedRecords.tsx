import type { RelatedRecord } from "../../types";
import { scrollToRecord } from "../../lib/recordScroll";

/** Collapsed, one line each — modeled on SourcesList's <details> pattern. */
export default function RelatedRecords({ items }: { items: RelatedRecord[] }) {
  if (items.length === 0) return null;

  return (
    <details className="related-records">
      <summary>Related records ({items.length})</summary>
      <ul>
        {items.map((r, i) => (
          <li key={i}>
            {r.recordId ? (
              <button type="button" className="record-id-link" onClick={() => scrollToRecord(r.recordId)}>
                [{r.recordId}]
              </button>
            ) : null}{" "}
            {r.oneLine}
          </li>
        ))}
      </ul>
    </details>
  );
}
