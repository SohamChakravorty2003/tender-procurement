import { useCallback, useEffect, useState } from "react";
import { getReviews, resolveReview } from "../api";
import { Badge } from "./Badges";

export default function ReviewsView({ onChanged, onOpenTender }) {
  const [rows, setRows] = useState(null);
  const [error, setError] = useState(null);
  const [busyId, setBusyId] = useState(null);

  const load = useCallback(() => {
    getReviews("pending").then(setRows).catch((e) => setError(e.message));
  }, []);
  useEffect(load, [load]);

  async function decide(id, action, tenderId) {
    setBusyId(id);
    setError(null);
    try {
      await resolveReview(id, action, tenderId);
      load();
      onChanged?.();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusyId(null);
    }
  }

  return (
    <div className="page page--narrow">
      <header className="page__head">
        <div>
          <h1>Reviews</h1>
          <p className="muted">Links the system would not make on its own. Nothing is merged until you decide.</p>
        </div>
      </header>

      {error && <div className="notice notice--error">{error}</div>}
      {!rows && !error && <p className="muted">Loading…</p>}
      {rows && rows.length === 0 && <p className="empty">Nothing waiting for review.</p>}

      {rows?.map((r) => (
        <div key={r.id} className="card">
          <div className="row">
            <Badge tone="amber">{r.review_type === "conflict" ? "Conflict" : "Suggested link"}</Badge>
            <strong>{r.source_file_name || `${r.source_kind} ${r.source_id}`}</strong>
          </div>
          <p className="muted">{r.reason}</p>
          <p>
            Currently in:{" "}
            {r.source_current_tender ? (
              <button className="chip" onClick={() => onOpenTender(r.source_current_tender)}>{r.source_current_tender}</button>
            ) : (
              <strong>no tender</strong>
            )}
          </p>
          <div className="row">
            <span>Put it in:</span>
            {[r.tender_id, r.other_tender_id].filter(Boolean).map((t) => (
              <button key={t} className="btn btn--small btn--primary" disabled={busyId === r.id}
                      onClick={() => decide(r.id, "confirm", t)}>
                {t}
              </button>
            ))}
            <button className="btn btn--small" disabled={busyId === r.id} onClick={() => decide(r.id, "reject", null)}>
              Keep as is (reject)
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}
