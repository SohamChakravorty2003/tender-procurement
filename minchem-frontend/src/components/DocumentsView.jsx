import { useCallback, useEffect, useState } from "react";
import { getDocument, getDocuments, retryProcessingJob } from "../api";
import { Badge } from "./Badges";
import UploadBox from "./UploadBox";

function stateTone(state) {
  return state === "completed" ? "green" : state === "failed" ? "red" : "amber";
}

function WindowSummary({ window }) {
  const detail = window.detail || {};
  const events = window.negotiation_events?.events || [];
  const lineCount = detail.lines?.length ?? 0;
  const componentCount = detail.components?.length ?? 0;
  const messageCount = detail.messages?.length ?? 0;
  return <details className="document-window">
    <summary>Pages {window.page_numbers.join(", ")} · {lineCount} lines · {componentCount} components · {messageCount} messages · {events.length} events</summary>
    {events.map((event) => <div className="document-event" key={`${event.message_index}-${event.event_index}`}>
      <Badge tone="blue">{event.event_type}</Badge> <strong>{event.vendor_name_raw || event.actor_party}</strong>
      <div className="muted small">{event.raw_event_text}</div>
      {event.commercial_lines?.map((line) => <div className="document-line" key={line.line_index}>{line.product_raw || "unspecified"} · {line.raw_price_text || "price not stated"} · {line.raw_price_terms || "terms not stated"}</div>)}
    </div>)}
    {(detail.lines || []).map((line) => <div className="document-line" key={line.line_index}>{line.product_raw || "unspecified"} · {line.quantity_raw || "quantity not stated"} · {line.unit_price ?? "price not stated"} {detail.currency || line.price_currency || ""}</div>)}
    {(detail.components || []).map((component, index) => <div className="document-line" key={`${component.component_type}-${index}`}>{component.component_type} · pages {component.start_page}-{component.end_page} · {component.bill_of_lading_number || component.invoice_number || "no printed reference"}</div>)}
  </details>;
}

function DocumentInspector({ document, onClose }) {
  const classification = document.analysis?.classification;
  return <aside className="document-inspector card">
    <div className="document-inspector__head"><div><h2>{document.file_name}</h2><p className="muted small">Document #{document.id} · {document.sha256}</p></div><button className="btn btn--small" onClick={onClose}>Close</button></div>
    <div className="row"><Badge tone={stateTone(document.processing_state)}>{document.processing_state}</Badge>{document.document_type && <Badge tone="blue">{document.document_type}</Badge>}{document.tender_associations.map((association) => <Badge tone="green" key={association.tender_id}>{association.tender_id} · {association.method}</Badge>)}</div>
    {classification && <section><h3>Classification evidence</h3><p>{classification.evidence?.map((item) => `p${item.page_number}: ${item.evidence_text}`).join(" · ") || "No evidence returned"}</p></section>}
    <section><h3>References</h3>{document.references.length ? <div className="document-list">{document.references.map((reference) => <div key={`${reference.reference_type}-${reference.raw_value}`}><Badge>{reference.reference_type}</Badge> <strong>{reference.raw_value}</strong>{reference.confidence != null && <span className="muted small"> · clarity {Math.round(reference.confidence * 100)}%</span>}<span className="muted small"> · {reference.evidence?.map((item) => `p${item.page_number}: ${item.evidence_text}`).join(" · ")}</span></div>)}</div> : <p className="muted">No typed references extracted.</p>}</section>
    <section><h3>Pages</h3><div className="row">{document.pages.map((page) => <Badge key={page.page_number}>page {page.page_number} · {page.width_px}×{page.height_px}</Badge>)}</div></section>
    <section><h3>Extraction windows</h3>{document.extraction_windows.length ? document.extraction_windows.map((window, index) => <WindowSummary window={window} key={index} />) : <p className="muted">Awaiting a completed analysis.</p>}</section>
    <section><h3>Grouping</h3>{document.tender_associations.length ? document.tender_associations.map((association) => <p key={association.tender_id}><strong>{association.tender_id}</strong> · {association.method === "typed_reference" ? "matched an existing tender through a shared typed reference" : association.method === "generated_typed_reference" ? "generated from this document group’s shared typed references" : association.method}</p>) : <p className="muted">No unique shared typed reference has confirmed a tender association.</p>}</section>
    <section><h3>Reviews</h3>{document.reviews.length ? document.reviews.map((review) => <div className="notice" key={review.id}><Badge tone="amber">{review.review_type}</Badge> <span className="muted small">{review.review_type === "conflicting_reference" ? "Reference candidates conflict; no tender was merged automatically. " : review.review_type === "no_tender_id" ? "No unique typed-reference grouping was available. " : ""}{JSON.stringify(review.payload)}</span></div>) : <p className="muted">No review required.</p>}</section>
  </aside>;
}

export default function DocumentsView() {
  const [documents, setDocuments] = useState(null);
  const [selected, setSelected] = useState(null);
  const [filter, setFilter] = useState("all");
  const [error, setError] = useState(null);
  const load = useCallback(() => getDocuments().then(setDocuments).catch((e) => setError(e.message)), []);
  useEffect(() => { load(); const timer = setInterval(load, 5000); return () => clearInterval(timer); }, [load]);
  async function openDocument(id) { try { setSelected(await getDocument(id)); } catch (e) { setError(e.message); } }
  async function retry(document, event) { event.stopPropagation(); try { setError(null); await retryProcessingJob(document.job_id); await load(); } catch (e) { setError(e.message); } }
  const visible = (documents || []).filter((document) => filter === "all" || (filter === "review" ? !document.tender_ids || document.processing_state === "failed" : document.processing_state === filter));
  return <div className="page documents-page">
    <header className="page__head"><div><h1>Documents</h1><p className="muted">Upload a fresh PDF set, then inspect image-first classification, evidence, references, grouping, and review status.</p></div><button className="btn" onClick={load}>Refresh</button></header>
    <UploadBox onDone={load} />
    <div className="pills">{[["all", "All"], ["queued", "Queued"], ["analyzing", "Analysing"], ["completed", "Completed"], ["review", "Needs review"], ["failed", "Failed"]].map(([value, label]) => <button key={value} className={`pill ${filter === value ? "is-active" : ""}`} onClick={() => setFilter(value)}>{label}</button>)}</div>
    {error && <div className="notice notice--error">{error}</div>}
    {!documents && !error && <p className="muted">Loading...</p>}
    {documents && visible.length === 0 && <p className="empty">No documents match this view.</p>}
    {visible.length > 0 && <div className="documents-layout"><div className="card card--flush"><table className="table"><thead><tr><th>Document</th><th>Type</th><th>Tender</th><th>State</th><th className="num">Pages</th></tr></thead><tbody>{visible.map((document) => <tr className={`is-click ${selected?.id === document.id ? "is-selected" : ""}`} key={document.id} onClick={() => openDocument(document.id)}><td><strong>{document.file_name}</strong><br /><span className="muted small">#{document.id}</span></td><td>{document.document_type || "-"}</td><td>{document.tender_ids || <span className="muted">review</span>}</td><td><Badge tone={stateTone(document.processing_state)}>{document.processing_state}</Badge>{document.processing_state === "failed" && <><br /><button className="btn btn--small btn--primary" onClick={(event) => retry(document, event)}>Retry</button></>}</td><td className="num">{document.page_count ?? "-"}</td></tr>)}</tbody></table></div>{selected && <DocumentInspector document={selected} onClose={() => setSelected(null)} />}</div>}
  </div>;
}
