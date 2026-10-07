import { useCallback, useEffect, useRef, useState } from "react";
import { deleteTender, getTenders, previewTenderDeletion } from "../api";
import { Badge } from "./Badges";
import UploadBox from "./UploadBox";

export default function TendersView({ onOpenTender }) {
  const [rows, setRows] = useState(null);
  const [error, setError] = useState(null);
  const [deleteTarget, setDeleteTarget] = useState(null);
  const [deleteConfirmation, setDeleteConfirmation] = useState("");
  const [deleteError, setDeleteError] = useState(null);
  const [deleting, setDeleting] = useState(false);
  const [message, setMessage] = useState(null);
  const messageTimer = useRef(null);

  const load = useCallback(() => {
    getTenders().then(setRows).catch((e) => setError(e.message));
  }, []);
  useEffect(load, [load]);
  useEffect(() => () => clearTimeout(messageTimer.current), []);

  async function openDeletePreview(tenderId) {
    setDeleteConfirmation("");
    setDeleteError(null);
    setDeleteTarget({ tenderId, records: null });
    try {
      const preview = await previewTenderDeletion(tenderId);
      setDeleteTarget({ tenderId, records: preview.records });
    } catch (e) {
      setDeleteError(e.message);
    }
  }

  async function confirmDelete() {
    if (!deleteTarget || deleteConfirmation !== deleteTarget.tenderId) return;
    setDeleting(true);
    setDeleteError(null);
    try {
      await deleteTender(deleteTarget.tenderId, deleteConfirmation);
      setMessage(`Deleted tender ${deleteTarget.tenderId} and its local database records.`);
      clearTimeout(messageTimer.current);
      messageTimer.current = setTimeout(() => setMessage(null), 15000);
      setDeleteTarget(null);
      setDeleteConfirmation("");
      load();
    } catch (e) {
      setDeleteError(e.message);
    } finally {
      setDeleting(false);
    }
  }

  return (
    <div className="page">
      <header className="page__head">
        <div>
          <h1>Tenders</h1>
          <p className="muted">Each tender groups the emails, documents and supplier offers of one deal.</p>
        </div>
      </header>

      <UploadBox onDone={load} onOpenTender={onOpenTender} />

      {error && <div className="notice notice--error">{error}</div>}
      {message && <div className="notice notice--success">{message}</div>}
      {!rows && !error && <p className="muted">Loading…</p>}
      {rows && rows.length === 0 && <p className="empty">No tenders yet. Upload a PDF to start one.</p>}

      {rows && rows.length > 0 && (
        <div className="card card--flush">
          <table className="table">
            <thead>
              <tr>
                <th>Tender</th>
                <th>Id source</th>
                <th className="num">Documents</th>
                <th className="num">Emails</th>
                <th className="num">Offers</th>
                <th className="num">Accepted</th>
                <th><span className="sr-only">Actions</span></th>
              </tr>
            </thead>
            <tbody>
              {rows.map((t) => (
                <tr key={t.tender_id} className="is-click" onClick={() => onOpenTender(t.tender_id)}>
                  <td><strong>{t.tender_id}</strong></td>
                  <td>
                    <Badge tone={t.id_origin === "generated" ? "grey" : "blue"}>
                      {t.id_origin === "generated" ? "generated" : "from documents"}
                    </Badge>
                  </td>
                  <td className="num">{t.document_count}</td>
                  <td className="num">{t.email_count}</td>
                  <td className="num">{t.bid_count}</td>
                  <td className="num">{t.accepted_count > 0 ? <Badge tone="green">{t.accepted_count}</Badge> : "—"}</td>
                  <td className="tender-actions">
                    <button className="btn btn--small btn--danger" type="button"
                            aria-label={`Delete tender ${t.tender_id}`}
                            onClick={(event) => { event.stopPropagation(); openDeletePreview(t.tender_id); }}>
                      Delete
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {deleteTarget && (
        <div className="confirm-backdrop">
          <section className="confirm-dialog card" role="dialog" aria-modal="true" aria-labelledby="delete-tender-title">
            <h2 id="delete-tender-title">Delete tender {deleteTarget.tenderId}?</h2>
            {deleteTarget.records ? (
              <>
                <p>This permanently removes these records from MinChem’s database:</p>
                <ul className="delete-counts">
                  <li>{deleteTarget.records.tenders} tender record</li>
                  <li>{deleteTarget.records.emails} email analysis record(s)</li>
                  <li>{deleteTarget.records.documents} linked document record(s), including extracted text</li>
                  <li>{deleteTarget.records.offers} offer/bid record(s)</li>
                  <li>{deleteTarget.records.references} tender reference(s)</li>
                  <li>{deleteTarget.records.reviews} related review record(s)</li>
                </ul>
                <p className="muted small">Original Gmail messages and source PDF files are not deleted. Gmail sync or document processing may add records again later.</p>
                <label className="delete-confirm-label">
                  Type <strong>{deleteTarget.tenderId}</strong> to confirm
                  <input className="input" autoFocus value={deleteConfirmation}
                         onChange={(event) => setDeleteConfirmation(event.target.value)} />
                </label>
              </>
            ) : <p className="muted">Loading deletion preview…</p>}
            {deleteError && <div className="notice notice--error">{deleteError}</div>}
            <div className="confirm-actions">
              <button className="btn" type="button" disabled={deleting}
                      onClick={() => { setDeleteTarget(null); setDeleteError(null); }}>
                Cancel
              </button>
              <button className="btn btn--danger" type="button" disabled={
                deleting || !deleteTarget.records || deleteConfirmation !== deleteTarget.tenderId
              } onClick={confirmDelete}>
                {deleting ? "Deleting…" : "Permanently delete tender"}
              </button>
            </div>
          </section>
        </div>
      )}
    </div>
  );
}
