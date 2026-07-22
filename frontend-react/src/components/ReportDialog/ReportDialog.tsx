import { useEffect, useState } from "react";
import { API_BASE } from "../../lib/config";

export type ReportKind = "incorrect_location" | "suggest_change" | "other";

export interface ReportPrefill {
  kind?: ReportKind;
  application_id?: string;
  location?: string;
  current_value?: string;
  suggested_value?: string;
  details?: string;
}

interface ReportDialogProps {
  open: boolean;
  onClose: () => void;
  prefill?: ReportPrefill | null;
}

export default function ReportDialog({ open, onClose, prefill }: ReportDialogProps) {
  const [kind, setKind] = useState<ReportKind>("incorrect_location");
  const [applicationId, setApplicationId] = useState("");
  const [location, setLocation] = useState("");
  const [currentValue, setCurrentValue] = useState("");
  const [suggestedValue, setSuggestedValue] = useState("");
  const [details, setDetails] = useState("");
  const [email, setEmail] = useState("");
  const [status, setStatus] = useState<{ text: string; ok?: boolean }>({ text: "" });
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open) return;
    setKind(prefill?.kind || "incorrect_location");
    setApplicationId(prefill?.application_id || "");
    setLocation(prefill?.location || "");
    setCurrentValue(prefill?.current_value || "");
    setSuggestedValue(prefill?.suggested_value || "");
    setDetails(prefill?.details || "");
    setStatus({ text: "" });
  }, [open, prefill]);

  if (!open) return null;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (details.trim().length < 5) {
      setStatus({ text: "Please add a few more details.", ok: false });
      return;
    }
    setBusy(true);
    setStatus({ text: "Sending…" });
    try {
      const res = await fetch(`${API_BASE}/reports`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          kind,
          application_id: applicationId.trim(),
          location: location.trim(),
          current_value: currentValue.trim(),
          suggested_value: suggestedValue.trim(),
          details: details.trim(),
          contact_email: email.trim(),
          page_url: window.location.href,
        }),
      });
      const data = await res.json().catch(() => ({} as { detail?: string }));
      if (!res.ok) throw new Error(data.detail || `Could not send (${res.status})`);
      setStatus({ text: "Thanks — your report was submitted.", ok: true });
      setTimeout(onClose, 900);
    } catch (err) {
      setStatus({
        text: err instanceof Error ? err.message : "Could not reach the server.",
        ok: false,
      });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="report-backdrop" role="presentation" onClick={onClose}>
      <div
        className="report-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="report-dialog-title"
        onClick={(e) => e.stopPropagation()}
      >
        <form onSubmit={submit}>
          <header className="report-dialog-head">
            <h2 id="report-dialog-title">Report a data issue</h2>
            <button type="button" className="report-close" onClick={onClose} aria-label="Close">
              ×
            </button>
          </header>
          <p className="report-intro">
            Flag an incorrect location or suggest a change. Reports go to the Engage Estero admin
            inbox.
          </p>
          <label className="report-label" htmlFor="report-kind">
            What is this about?
          </label>
          <select
            id="report-kind"
            value={kind}
            onChange={(e) => setKind(e.target.value as ReportKind)}
            required
          >
            <option value="incorrect_location">Incorrect location</option>
            <option value="suggest_change">Suggest a change</option>
            <option value="other">Other</option>
          </select>
          <label className="report-label" htmlFor="report-application-id">
            Application ID (optional)
          </label>
          <input
            id="report-application-id"
            value={applicationId}
            onChange={(e) => setApplicationId(e.target.value)}
            maxLength={120}
            placeholder="e.g. DOS2022-E016"
          />
          <label className="report-label" htmlFor="report-location">
            Location (optional)
          </label>
          <input
            id="report-location"
            value={location}
            onChange={(e) => setLocation(e.target.value)}
            maxLength={500}
            placeholder="Address or place shown in the answer"
          />
          <label className="report-label" htmlFor="report-current">
            What does it say now? (optional)
          </label>
          <input
            id="report-current"
            value={currentValue}
            onChange={(e) => setCurrentValue(e.target.value)}
            maxLength={1000}
          />
          <label className="report-label" htmlFor="report-suggested">
            What should it say? (optional)
          </label>
          <input
            id="report-suggested"
            value={suggestedValue}
            onChange={(e) => setSuggestedValue(e.target.value)}
            maxLength={1000}
          />
          <label className="report-label" htmlFor="report-details">
            Details
          </label>
          <textarea
            id="report-details"
            value={details}
            onChange={(e) => setDetails(e.target.value)}
            required
            minLength={5}
            maxLength={4000}
            placeholder="Describe the problem or suggested correction…"
          />
          <label className="report-label" htmlFor="report-email">
            Email if we may follow up (optional)
          </label>
          <input
            id="report-email"
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            maxLength={254}
            autoComplete="email"
          />
          <div className="report-dialog-actions">
            <button type="button" className="btn-dir" onClick={onClose}>
              Cancel
            </button>
            <button type="submit" className="btn-minutes" disabled={busy}>
              Submit report
            </button>
          </div>
          {status.text && (
            <p
              className={`report-form-status${
                status.ok === true ? " ok" : status.ok === false ? " err" : ""
              }`}
            >
              {status.text}
            </p>
          )}
        </form>
      </div>
    </div>
  );
}
