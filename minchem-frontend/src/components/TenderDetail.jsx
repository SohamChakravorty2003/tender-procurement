import { Fragment, useEffect, useState } from "react";
import { extractTenderOffers, getTender, getThread, processTenderAttachments } from "../api";
import { Badge, CategoryBadge, RoleBadge } from "./Badges";
import PriceChart from "./PriceChart";
import ThreadView from "./ThreadView";
import { docTypeLabel, formatOffer, prettyWord, senderName } from "../constants";

function TenderEmailPreview({ email, showThread }) {
  const [messages, setMessages] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    setMessages(null);
    setError(null);
    if (!email.thread_id) {
      setError("This email does not have a conversation thread available.");
      return () => { cancelled = true; };
    }
    getThread(email.thread_id)
      .then((thread) => !cancelled && setMessages(thread))
      .catch((e) => !cancelled && setError(e.message));
    return () => { cancelled = true; };
  }, [email.email_id, email.thread_id]);

  if (error) return <div className="notice notice--error">{error}</div>;
  if (!messages) return <p className="muted">Loading email…</p>;

  const message = messages.find((item) => item.id === email.email_id);
  if (!message) return <p className="muted">Email content is unavailable in this conversation.</p>;

  return (
    <div className="tender-email-preview">
      <h3>{message.subject || email.subject || "(no subject)"}</h3>
      <dl className="tender-email-preview__meta">
        <div><dt>From</dt><dd>{message.sender || email.sender || "—"}</dd></div>
        <div><dt>To</dt><dd>{message.recipient || "—"}</dd></div>
        <div><dt>Date</dt><dd>{message.date || "—"}</dd></div>
      </dl>
      {!showThread && <pre className="tender-email-preview__body">{message.body || "(no text)"}</pre>}
      {message.attachments?.length > 0 && (
        <div className="row">
          {message.attachments.map((name) => <Badge key={name} tone="purple">{name}</Badge>)}
        </div>
      )}
      {showThread && <ThreadView threadId={email.thread_id} initialMessages={messages} />}
    </div>
  );
}

export default function TenderDetail({ tenderId, onBack }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [extracting, setExtracting] = useState(false);
  const [extractError, setExtractError] = useState(null);
  const [extractMessage, setExtractMessage] = useState(null);
  const [processingAttachments, setProcessingAttachments] = useState(false);
  const [attachmentError, setAttachmentError] = useState(null);
  const [attachmentMessage, setAttachmentMessage] = useState(null);
  const [openEmailId, setOpenEmailId] = useState(null);
  const [showEmailThread, setShowEmailThread] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setData(null);
    setError(null);
    setExtractError(null);
    setExtractMessage(null);
    setAttachmentError(null);
    setAttachmentMessage(null);
    setOpenEmailId(null);
    setShowEmailThread(false);
    getTender(tenderId)
      .then((d) => !cancelled && setData(d))
      .catch((e) => !cancelled && setError(e.message));
    return () => { cancelled = true; };
  }, [tenderId]);

  const back = <button className="btn btn--small" onClick={onBack}>← All tenders</button>;
  if (error) return <div className="page">{back}<div className="notice notice--error">{error}</div></div>;
  if (!data) return <div className="page">{back}<p className="muted">Loading…</p></div>;

  const { tender, emails, documents, bids } = data;
  const offers = bids.filter((b) => b.offer);
  const pendingBids = bids.filter((b) => !b.offer);
  const docCount = Object.values(documents).reduce((n, list) => n + list.length, 0);
  const accepted = offers.filter((b) => b.accepted).length;
  const offerNumbers = new Map(offers.map((b, index) => [b.id, index + 1]));

  async function handleExtractOffers() {
    setExtracting(true);
    setExtractError(null);
    setExtractMessage(null);
    try {
      const result = await extractTenderOffers(tenderId);
      setData(await getTender(tenderId));
      setExtractMessage(
        `Checked ${result.attachment_emails_checked} email(s) for PDF attachments; ` +
        `processed ${result.attachment_pdfs_processed} attachment PDF(s); ` +
        `${result.attachment_reviews_queued} attachment link review(s) queued; ` +
        `registered ${result.emails_registered} missing vendor email(s); ` +
        `extracted ${result.extracted} email(s); saved ${result.thread_offers_saved} offer(s) from ` +
        `${result.thread_documents_processed} linked PDF(s); ` +
        `${result.thread_documents_without_offers} PDF(s) had no offers; ` +
        `${result.failed + result.thread_documents_failed + result.email_registration_failed + result.attachment_failed} step(s) failed; ` +
        `${result.duplicates_marked} duplicate offer(s) marked.`
      );
      const errors = [...(result.email_registration_errors || []), ...(result.thread_errors || [])];
      if (result.attachment_failed) errors.push(`${result.attachment_failed} PDF attachment step(s) failed; retry or check the backend log.`);
      if (errors.length) setExtractError(errors.join(" "));
    } catch (e) {
      setExtractError(e.message);
    } finally {
      setExtracting(false);
    }
  }

  async function handleProcessAttachments() {
    setProcessingAttachments(true);
    setAttachmentError(null);
    setAttachmentMessage(null);
    try {
      const result = await processTenderAttachments(tenderId);
      setData(await getTender(tenderId));
      const skipped = result.skipped_non_pdf + result.skipped_too_large;
      setAttachmentMessage(
        result.attachments_found === 0
          ? `Checked ${result.emails_checked} tender email(s); no attachments found.`
          : `Processed ${result.pdfs_processed} PDF(s); updated ${result.classification_updated} email classification(s); ` +
            `${result.failed} failed; ${result.document_analysis_failed} document analysis failure(s); ` +
            `${skipped} attachment(s) skipped; ${result.reviews_queued} link conflict(s) queued for review.`
      );
    } catch (e) {
      setAttachmentError(e.message);
    } finally {
      setProcessingAttachments(false);
    }
  }

  const registeredEmailIds = new Set(bids.map((b) => b.email_id).filter(Boolean));
  const missingEmailIds = emails.filter((email) =>
    email.status === "ok" && email.email_role === "vendor_bid" && !registeredEmailIds.has(email.email_id)
  ).map((email) => email.email_id);
  const extractableCount = new Set([...missingEmailIds, ...bids.filter((b) =>
    b.email_id && !b.offer && ["pending", "failed"].includes(b.extraction_status)
  ).map((b) => b.email_id)]).size;
  const documentsWithOffers = new Set(bids.map((b) => b.document_id).filter(Boolean));
  const offerDocuments = [...(documents.email_thread || []), ...(documents.quotation || [])];
  const extractablePdfCount = offerDocuments.filter((doc) =>
    !documentsWithOffers.has(doc.id) && doc.offer_extraction_status !== "no_offers"
  ).length;
  const attachmentScanCount = emails.filter((email) => email.status === "ok" && !email.attachments_checked_at).length;
  const hasNewOfferSources = extractableCount > 0 || extractablePdfCount > 0 || attachmentScanCount > 0;

  return (
    <div className="page">
      {back}
      <header className="page__head">
        <div>
          <h1>{tender.tender_id}</h1>
          <p className="muted">
            {emails.length} emails · {docCount} documents · {offers.filter((b) => !b.duplicate_of).length} offers
            {accepted > 0 && ` · ${accepted} accepted`}
          </p>
        </div>
        <Badge tone={tender.id_origin === "generated" ? "grey" : "blue"}>
          {tender.id_origin === "generated" ? "generated id" : "id from documents"}
        </Badge>
      </header>

      <section>
        <div className="row offers__heading">
          <h2>Offers</h2>
          <button className="btn btn--small" onClick={handleExtractOffers}
                  disabled={extracting || !hasNewOfferSources}
                  title={hasNewOfferSources ? "Check linked emails and PDF attachments, extract email and PDF offers, then update duplicate and accepted marks" : "No new linked email or PDF sources"}>
            {extracting ? "Extracting offers…" : hasNewOfferSources ? `Extract offers (${extractableCount} email, ${extractablePdfCount} PDF${attachmentScanCount ? `, ${attachmentScanCount} attachment check` : ""})` : offers.length ? "Offers up to date" : "No offers to extract"}
          </button>
          <button className="btn btn--small" onClick={handleProcessAttachments}
                  disabled={processingAttachments}
                  title="Download and process PDF attachments from emails assigned to this tender">
            {processingAttachments ? "Processing PDFs…" : "Process PDF attachments"}
          </button>
        </div>
        {extractError && <div className="notice notice--error">{extractError}</div>}
        {extractMessage && <p className="muted small">{extractMessage}</p>}
        {attachmentError && <div className="notice notice--error">{attachmentError}</div>}
        {attachmentMessage && <p className="muted small">{attachmentMessage}</p>}
        {offers.length === 0 ? (
          <p className="empty">No offers extracted for this tender.</p>
        ) : (
          <>
            <div className="card price-analysis">
              <h3>Offer Analysis</h3>
              <section className="price-analysis__section">
                <h4>Supplier Replies &amp; Price Tiers</h4>
                <PriceChart offers={offers} />
              </section>
            </div>
            <div className="card card--flush">
              <table className="table">
                <thead>
                  <tr><th>Offer #</th><th>Date</th><th>Vendor</th><th className="num">Price</th><th>Basis</th><th>Grade</th><th>Kind</th><th></th></tr>
                </thead>
                <tbody>
                  {offers.map((b) => (
                    <tr key={b.id} className={`${b.duplicate_of ? "is-dup" : ""} ${b.accepted ? "is-accepted" : ""}`}>
                      <td className="nowrap">{offerNumbers.get(b.id)}</td>
                      <td className="nowrap">{b.offered_at || "—"}</td>
                      <td>{senderName(b.vendor_name) || b.vendor_email || "—"}</td>
                      <td className="num"><strong>{formatOffer(b.offer)}</strong></td>
                      <td>{b.offer.price_terms || "—"}</td>
                      <td>{b.offer.specifications || "—"}</td>
                      <td>{b.offer.kind === "vendor_acceptance" ? "acceptance" : "offer"}</td>
                      <td className="nowrap">
                        {b.accepted && <Badge tone="green">Accepted</Badge>}{" "}
                        {b.duplicate_of && <Badge tone="amber" title={`Bid record #${b.duplicate_of}`}>
                          Duplicate of Offer #{offerNumbers.get(b.duplicate_of) ?? `bid ${b.duplicate_of}`}
                        </Badge>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="muted small">
              The accepted offer is the one whose price appears in the sales contract. It shows the winning terms, not necessarily the cheapest offer.
            </p>
          </>
        )}
        {pendingBids.length > 0 && (
          <p className="muted small">
            {pendingBids.length} email bid(s) registered but not extracted yet:{" "}
            {pendingBids.map((b) => senderName(b.vendor_name) || b.vendor_email || `#${b.id}`).join(", ")}.
          </p>
        )}
      </section>

      <section>
        <h2>Documents</h2>
        {docCount === 0 && <p className="empty">No documents linked.</p>}
        <div className="grid">
          {Object.entries(documents).map(([type, docs]) => (
            <div key={type} className="card">
              <div className="card__title">{docTypeLabel(type)} <span className="muted">({docs.length})</span></div>
              {docs.map((d) => (
                <div key={d.id} className="doc">
                  <div className="doc__name" title={d.file_name}>{d.file_name}</div>
                  <div className="row">
                    {d.contains.map((c) => <Badge key={c}>{prettyWord(c)}</Badge>)}
                    <Badge tone={d.extraction_status === "ok" ? "green" : "amber"}
                           title={d.extraction_status === "ok" ? "Every page was read" : "Some pages may be unreliable"}>
                      text {d.extraction_status}
                    </Badge>
                    {["email_thread", "quotation"].includes(type) && d.offer_extraction_status === "no_offers" &&
                      <Badge tone="grey">No supplier offer found</Badge>}
                    {["email_thread", "quotation"].includes(type) && d.offer_extraction_status === "failed" &&
                      <Badge tone="amber">Offer extraction failed</Badge>}
                    <span className="muted small">{d.page_count} p · linked by {d.link_method || "—"}</span>
                  </div>
                </div>
              ))}
            </div>
          ))}
        </div>
      </section>

      <section>
        <div className="row tender-emails__heading">
          <h2>Emails</h2>
          {openEmailId && emails.find((email) => email.email_id === openEmailId)?.thread_id && (
            <button className="btn btn--small" onClick={() => setShowEmailThread((value) => !value)}>
              {showEmailThread ? "Hide entire thread" : "Show entire thread"}
            </button>
          )}
        </div>
        {emails.length === 0 ? (
          <p className="empty">No Gmail emails linked to this tender.</p>
        ) : (
          <div className="card card--flush">
            <table className="table">
              <tbody>
                {emails.map((e) => (
                  <Fragment key={e.email_id}>
                    <tr className="is-click" onClick={() => {
                      setOpenEmailId((id) => id === e.email_id ? null : e.email_id);
                      setShowEmailThread(false);
                    }}>
                      <td><button className="text-button tender-email__subject" aria-expanded={openEmailId === e.email_id}>
                        {e.subject || "(no subject)"}
                      </button></td>
                      <td className="muted">{senderName(e.sender)}</td>
                      <td className="nowrap"><CategoryBadge value={e.category} /> <RoleBadge value={e.email_role} /></td>
                    </tr>
                    {openEmailId === e.email_id && (
                      <tr key={`${e.email_id}-detail`}>
                      <td colSpan="3"><TenderEmailPreview email={e} showThread={showEmailThread} /></td>
                      </tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
