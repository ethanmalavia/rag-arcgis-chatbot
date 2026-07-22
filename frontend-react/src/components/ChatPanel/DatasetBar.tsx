import { useRef, useState } from "react";
import { API_BASE } from "../../lib/config";

type StatusKind = "idle" | "loading" | "success" | "error";

export default function DatasetBar() {
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [status, setStatus] = useState<{ text: string; kind: StatusKind }>({ text: "", kind: "idle" });

  const loadCSV = async () => {
    const file = fileInputRef.current?.files?.[0];
    if (!file) {
      setStatus({ text: "Please select a CSV file first.", kind: "error" });
      return;
    }
    setStatus({ text: "⏳ Loading…", kind: "loading" });
    const formData = new FormData();
    formData.append("file", file);
    try {
      const res = await fetch(`${API_BASE}/load`, { method: "POST", body: formData });
      const data = await res.json().catch(() => ({}) as { message?: string; detail?: string });
      if (!res.ok) {
        setStatus({ text: `❌ ${data.detail || `Server error ${res.status}`}`, kind: "error" });
      } else {
        setStatus({ text: `✅ ${data.message || "Loaded!"}`, kind: "success" });
      }
    } catch {
      setStatus({ text: `❌ Can't reach backend at ${API_BASE}`, kind: "error" });
    }
  };

  return (
    <div id="dataset-bar">
      <label htmlFor="csv-file-input">📂 Load CSV:</label>
      <input type="file" id="csv-file-input" accept=".csv" ref={fileInputRef} />
      <button type="button" id="load-btn" onClick={loadCSV}>
        Load Dataset
      </button>
      <span id="load-status" className={`status-${status.kind}`}>
        {status.text}
      </span>
    </div>
  );
}
