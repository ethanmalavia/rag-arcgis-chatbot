import { useState } from "react";

export default function DisclaimerModal() {
  const [open, setOpen] = useState(true);

  if (!open) return null;

  return (
    <div className="disclaimer-backdrop">
      <div className="disclaimer-dialog" role="dialog" aria-modal="true" aria-labelledby="disclaimer-title">
        <h2 id="disclaimer-title">Before you begin</h2>
        <p>
          This tool uses AI to summarize public records. It can make mistakes. Don&apos;t rely on it for financial or
          legal decisions.
        </p>
        <button type="button" className="btn-showmap disclaimer-ack-btn" onClick={() => setOpen(false)}>
          I understand
        </button>
      </div>
    </div>
  );
}
