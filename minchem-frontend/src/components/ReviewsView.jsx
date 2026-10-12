import { Fragment, useCallback, useEffect, useState } from "react";
import { assignV2ReviewCluster, getDocumentPdfUrl, getReviews, getTenders, getV2Reviews, reopenV2Review, resolveReview, resolveV2Review } from "../api";
import { Badge } from "./Badges";

export default function ReviewsView({ onChanged, onOpenTender }) {
  const [rows, setRows] = useState(null);
  const [documentRows, setDocumentRows] = useState(null);
  const [ungroupedRows, setUngroupedRows] = useState(null);
  const [documentIssueCount, setDocumentIssueCount] = useState(0);
  const [reviewTab, setReviewTab] = useState("grouping");
  const [tenders, setTenders] = useState([]);
  const [selectedTender, setSelectedTender] = useState({});
  const [previewIds, setPreviewIds] = useState(() => new Set());
  const [error, setError] = useState(null);
  const [busyId, setBusyId] = useState(null);
  const [reloading, setReloading] = useState(false);

  const load = useCallback(() => {
    Promise.all([getReviews("pending"), getV2Reviews("pending"), getV2Reviews("rejected"), getTenders()])
      .then(([linkRows, v2Rows, rejectedRows, tenderRows]) => {
        const oneReviewPerDocument = (reviews) => {
          const unique = new Map();
          for (const review of reviews) {
            if (review.document_id != null && !unique.has(review.document_id)) unique.set(review.document_id, review);
          }
          return [...unique.values()];
        };
        setRows(linkRows);
        setDocumentRows(oneReviewPerDocument(v2Rows));
        setUngroupedRows(oneReviewPerDocument(rejectedRows));
        setDocumentIssueCount(v2Rows.length);
        setTenders(tenderRows);
        setError(null);
      })
      .catch((e) => setError(e.message));
  }, []);
  useEffect(load, [load]);

  async function reload() {
    setReloading(true);
    await load();
    onChanged?.();
    setReloading(false);
  }

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

  async function decideDocument(id, action) {
    const tenderId = selectedTender[id];
    if (action === "confirm" && !tenderId) {
      setError("Choose a tender before merging the document.");
      return;
    }
    setBusyId(`document-${id}`);
    setError(null);
    try {
      await resolveV2Review(id, action, tenderId);
      if (action === "reject") setReviewTab("ungrouped");
      load();
      onChanged?.();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusyId(null);
    }
  }

  async function reopenDocument(id) {
    setBusyId(`reopen-${id}`);
    setError(null);
    try {
      await reopenV2Review(id);
      setReviewTab("grouping");
      load();
      onChanged?.();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusyId(null);
    }
  }

  async function assignCluster(id) {
    const tenderId = selectedTender[id];
    if (!tenderId) {
      setError("Choose a tender before assigning a reference cluster.");
      return;
    }
    setBusyId(`cluster-${id}`);
    setError(null);
    try {
      await assignV2ReviewCluster(id, tenderId);
      load();
      onChanged?.();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusyId(null);
    }
  }

  function documentReason(review) {
    if (review.review_type === "conflicting_upload_batch_tender") {
      const tenderIds = review.payload?.tender_ids || [];
      return `This upload includes more than one tender${tenderIds.length ? `: ${tenderIds.join(", ")}` : ""}.`;
    }
    if (review.review_type === "no_tender_id") return "No unique tender ID or shared typed reference was found.";
    if (review.review_type === "conflicting_reference") return "The extracted references point to more than one possible tender.";
    if (review.review_type === "multiple_tender_ids") return "More than one tender ID was extracted from this document.";
    return review.review_type.replaceAll("_", " ");
  }

  return (
    <div className="page">
      <header className="page__head">
        <div>
          <h1>Reviews</h1>
          <p className="muted">Documents stay ungrouped until their evidence is clear or you choose the correct tender.</p>
          {documentRows && <p className="muted small">{documentRows.length} document{documentRows.length === 1 ? "" : "s"} need a decision across {documentIssueCount} open issue{documentIssueCount === 1 ? "" : "s"}. A PDF can have more than one issue.</p>}
        </div>
        <button className="btn btn--small" type="button" disabled={reloading} onClick={reload}>
          {reloading ? "Reloading…" : "Reload reviews"}
        </button>
      </header>

      {error && <div className="notice notice--error">{error}</div>}
      {!rows && !error && <p className="muted">Loading…</p>}
      {rows && documentRows && ungroupedRows && rows.length === 0 && documentRows.length === 0 && ungroupedRows.length === 0 && <p className="empty">Nothing waiting for review.</p>}

      <div className="review-tabs" role="tablist" aria-label="Document reviews">
        <button className={`btn btn--small ${reviewTab === "grouping" ? "btn--primary" : ""}`} onClick={() => setReviewTab("grouping")}>Needs a decision ({documentRows?.length ?? 0} documents)</button>
        <button className={`btn btn--small ${reviewTab === "ungrouped" ? "btn--primary" : ""}`} onClick={() => setReviewTab("ungrouped")}>Kept ungrouped ({ungroupedRows?.length ?? 0} decisions)</button>
      </div>

      {reviewTab === "grouping" && documentRows?.length > 0 && (
        <section>
          <h2>Document grouping</h2>
          <p className="muted">Choose a tender in the Tender column, then merge the individual PDF. This human decision replaces any generated grouping for that PDF.</p>
          <div className="card card--flush">
            <table className="table review-table">
              <thead><tr><th>Document</th><th>Reason</th><th>Extracted references</th><th>Tender</th></tr></thead>
              <tbody>
                {documentRows.map((r) => (
                  <Fragment key={`v2-${r.id}`}>
                  <tr>
                    <td><strong>{r.file_name || `Document ${r.document_id}`}</strong><div className="muted">{r.document_type?.replaceAll("_", " ") || "document"}{r.page_count ? ` · ${r.page_count} pages` : ""}</div></td>
                    <td><Badge tone="amber">Needs review</Badge><div className="muted review-table__reason">{documentReason(r)}</div></td>
                    <td>
                      {r.references?.length ? r.references.map((ref) => <span className="chip review-table__ref" key={`${ref.reference_type}-${ref.raw_value}`}>{ref.reference_type.replaceAll("_", " ")}: {ref.raw_value}{ref.evidence?.[0]?.page_number ? ` (p. ${ref.evidence[0].page_number})` : ""}</span>) : <span className="muted">None extracted</span>}
                      {r.linked_documents?.length > 0 && <div className="muted review-table__evidence">Shared with: {r.linked_documents.map((item) => item.file_name).join(", ")}</div>}
                    </td>
                    <td>
                      <select className="input review-table__select" value={selectedTender[r.id] || ""} onChange={(event) => setSelectedTender((current) => ({ ...current, [r.id]: event.target.value }))}>
                        <option value="">Choose tender…</option>
                        {tenders.map((tender) => <option key={tender.tender_id} value={tender.tender_id}>{tender.tender_id}{tender.title ? ` — ${tender.title}` : ""}</option>)}
                      </select>
                      {r.tender_candidates?.length > 0 && <div className="muted review-table__evidence">Evidence suggests: {r.tender_candidates.join(", ")}</div>}
                      <div className="row review-table__actions">
                        <button className="btn btn--small btn--primary" disabled={busyId === `document-${r.id}`} onClick={() => decideDocument(r.id, "confirm")}>Merge</button>
                        {r.linked_documents?.length > 0 && <button className="btn btn--small" disabled={busyId === `cluster-${r.id}`} onClick={() => assignCluster(r.id)}>Assign reference cluster</button>}
                        <button className="btn btn--small" disabled={busyId === `document-${r.id}`} onClick={() => decideDocument(r.id, "reject")}>Keep ungrouped</button>
                      </div>
                    </td>
                  </tr>
                  <tr className="review-table__preview-row">
                    <td colSpan="4">
                      <details onToggle={(event) => {
                        if (event.currentTarget.open) setPreviewIds((current) => new Set(current).add(r.document_id));
                      }}>
                        <summary>Preview PDF</summary>
                        {previewIds.has(r.document_id) && <iframe className="review-pdf" src={getDocumentPdfUrl(r.document_id)} title={`PDF preview: ${r.file_name || r.document_id}`} />}
                      </details>
                    </td>
                  </tr>
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {reviewTab === "ungrouped" && ungroupedRows?.length === 0 && <p className="empty">No PDFs are being kept ungrouped.</p>}
      {reviewTab === "ungrouped" && ungroupedRows?.length > 0 && (
        <section>
          <h2>Kept ungrouped</h2>
          <p className="muted">These PDFs have no tender association because you chose to keep them separate. Bring one back when you are ready to review it again.</p>
          <div className="card card--flush">
            <table className="table review-table">
              <thead><tr><th>Document</th><th>Reason</th><th>Extracted references</th><th>Action</th></tr></thead>
              <tbody>
                {ungroupedRows.map((r) => (
                  <Fragment key={`ungrouped-${r.id}`}>
                    <tr>
                      <td><strong>{r.file_name || `Document ${r.document_id}`}</strong><div className="muted">{r.document_type?.replaceAll("_", " ") || "document"}{r.page_count ? ` · ${r.page_count} pages` : ""}</div></td>
                      <td><div className="muted review-table__reason">{documentReason(r)}</div></td>
                      <td>{r.references?.length ? r.references.map((ref) => <span className="chip review-table__ref" key={`${ref.reference_type}-${ref.raw_value}`}>{ref.reference_type.replaceAll("_", " ")}: {ref.raw_value}</span>) : <span className="muted">None extracted</span>}</td>
                      <td><button className="btn btn--small btn--primary" disabled={busyId === `reopen-${r.id}`} onClick={() => reopenDocument(r.id)}>Bring back to document grouping</button></td>
                    </tr>
                    <tr className="review-table__preview-row"><td colSpan="4"><details onToggle={(event) => { if (event.currentTarget.open) setPreviewIds((current) => new Set(current).add(r.document_id)); }}><summary>Preview PDF</summary>{previewIds.has(r.document_id) && <iframe className="review-pdf" src={getDocumentPdfUrl(r.document_id)} title={`PDF preview: ${r.file_name || r.document_id}`} />}</details></td></tr>
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {rows?.length > 0 && <h2>Other link reviews</h2>}

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
