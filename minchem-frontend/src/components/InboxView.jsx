import { useEffect, useMemo, useRef, useState } from "react";
import { CATEGORIES, senderName } from "../constants";
import { restoreDeletedThread } from "../api";
import { CategoryBadge, RoleBadge, TenderChip } from "./Badges";
import ThreadView from "./ThreadView";

function shortDate(value) {
  const d = new Date(value);
  if (isNaN(d)) return value || "";
  const today = new Date();
  return d.toDateString() === today.toDateString()
    ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleDateString([], { day: "numeric", month: "short" });
}

function EmailBody({ text }) {
  const lines = (text || "").split("\n");
  const sections = [];

  for (let i = 0; i < lines.length;) {
    const isQuoteLine = (line) => /^\s*>/.test(line);
    const isAttribution = (index) => {
      if (!/^\s*On\s/i.test(lines[index] || "")) return false;
      return lines.slice(index, index + 5).some((line) => /\bwrote:\s*$/i.test(line));
    };

    if (isQuoteLine(lines[i]) || isAttribution(i)) {
      const quoted = [];
      if (isAttribution(i)) {
        while (i < lines.length && !isQuoteLine(lines[i])) quoted.push(lines[i++]);
        while (quoted.length && !quoted[quoted.length - 1].trim()) quoted.pop();
      }
      while (i < lines.length && (isQuoteLine(lines[i]) || (!lines[i].trim() && quoted.length))) {
        quoted.push(lines[i++].replace(/^\s*>\s?/, ""));
      }
      sections.push({ quote: true, text: quoted.join("\n").trim() });
      continue;
    }

    const normal = [];
    while (i < lines.length && !isQuoteLine(lines[i]) && !isAttribution(i)) normal.push(lines[i++]);
    sections.push({ quote: false, text: normal.join("\n") });
  }

  return (
    <div className="mail-body">
      {sections.map((section, index) => section.quote ? (
        <blockquote className="mail-body__quote" key={index}>
          <span className="mail-body__label">Quoted history</span>
          <div>{section.text}</div>
        </blockquote>
      ) : (
        <div className="mail-body__text" key={index}>
          <div>{section.text}</div>
        </div>
      ))}
    </div>
  );
}

export default function InboxView({ emails, loaded, refreshing, error, onRefresh, onOpenTender }) {
  const [category, setCategory] = useState("all");
  const [query, setQuery] = useState("");
  const [selectedId, setSelectedId] = useState(null);
  const [showThread, setShowThread] = useState(false);
  const [restoring, setRestoring] = useState(false);
  const [restoreMessage, setRestoreMessage] = useState(null);
  const [restoreError, setRestoreError] = useState(null);
  const restoreMessageTimer = useRef(null);

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    return emails.filter((e) => {
      if (category !== "all" && e.category !== category) return false;
      if (!q) return true;
      return [e.subject, e.sender, e.tender_id, e.snippet].some((f) => (f || "").toLowerCase().includes(q));
    });
  }, [emails, category, query]);

  const selected = visible.find((e) => e.id === selectedId) || null;

  useEffect(() => {
    if (selectedId && !visible.some((e) => e.id === selectedId)) setSelectedId(null);
  }, [visible, selectedId]);

  // Picking another email closes the conversation view.
  useEffect(() => {
    setShowThread(false);
    setRestoreMessage(null);
    setRestoreError(null);
  }, [selectedId]);
  useEffect(() => () => clearTimeout(restoreMessageTimer.current), []);

  async function handleRestoreThread() {
    if (!selected?.thread_id) return;
    setRestoring(true);
    setRestoreMessage(null);
    setRestoreError(null);
    try {
      const result = await restoreDeletedThread(selected.id, selected.thread_id);
      await onRefresh();
      setRestoreMessage(result.tender_id
        ? `Restored ${result.tender_id}: analysed ${result.messages_analysed} message(s) (${result.analysis_failed} failed), extracted offers from ${result.offer_emails_extracted} email(s) (${result.offer_emails_failed} failed), processed ${result.pdfs_processed} PDF(s) (${result.pdfs_failed} failed), extracted ${result.thread_offers_saved} offer(s) from email-thread PDFs, and marked ${result.duplicate_offers_marked} duplicate(s).`
        : `Analysed ${result.messages_analysed} message(s), but no tender ID was recovered, so offers and PDFs were not processed.`);
      clearTimeout(restoreMessageTimer.current);
      restoreMessageTimer.current = setTimeout(() => setRestoreMessage(null), 15000);
    } catch (e) {
      setRestoreError(e.message);
    } finally {
      setRestoring(false);
    }
  }

  return (
    <div className="page page--inbox">
      <header className="page__head">
        <div>
          <h1>Inbox</h1>
          <p className="muted">
            {error && loaded ? "Last refresh failed. Showing the previous inbox." : `${visible.length} of ${emails.length} emails`}
          </p>
        </div>
        <button className="btn" onClick={onRefresh} disabled={refreshing}>
          {refreshing ? "Refreshing…" : "Refresh"}
        </button>
      </header>

      <div className="filters">
        <input
          className="input"
          placeholder="Search subject, sender or tender"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <div className="pills">
          {[{ value: "all", label: "All" }, ...CATEGORIES].map((c) => (
            <button
              key={c.value}
              className={`pill ${category === c.value ? "is-active" : ""}`}
              onClick={() => setCategory(c.value)}
            >
              {c.label}
            </button>
          ))}
        </div>
      </div>

      {!loaded && refreshing && <p className="muted">Loading inbox… new emails are classified on first load, which can take a while.</p>}
      {!loaded && error && (
        <div className="notice notice--error">
          {error} <button className="btn btn--small" onClick={onRefresh}>Retry</button>
        </div>
      )}
      {loaded && visible.length === 0 && <p className="empty">No emails match.</p>}

      {visible.length > 0 && (
        <div className={`split ${selected ? "has-detail" : ""}`}>
          <ul className="mail-list">
            {visible.map((e) => (
              <li key={e.id}>
                <button
                  className={`mail-row ${e.id === selectedId ? "is-active" : ""}`}
                  onClick={() => setSelectedId(e.id)}
                >
                  <div className="mail-row__top">
                    <span className="mail-row__from">{senderName(e.sender)}</span>
                    <span className="muted small">{shortDate(e.date)}</span>
                  </div>
                  <div className="mail-row__subject">{e.subject || "(no subject)"}</div>
                  <div className="mail-row__tags">
                    <CategoryBadge value={e.category} />
                    <RoleBadge value={e.email_role} />
                    <TenderChip id={e.tender_id} onOpen={onOpenTender} />
                  </div>
                </button>
              </li>
            ))}
          </ul>

          <section className="mail-detail">
            {!selected ? (
              <p className="empty">Select an email to read it.</p>
            ) : (
              <>
                <button className="btn btn--small mail-detail__back" onClick={() => setSelectedId(null)}>
                  Back to list
                </button>
                <h2>{selected.subject || "(no subject)"}</h2>
                <div className="mail-detail__meta">
                  <div><span className="muted">From</span> {selected.sender}</div>
                  <div><span className="muted">To</span> {selected.recipient}</div>
                  <div><span className="muted">Date</span> {selected.date}</div>
                </div>
                <div className="mail-row__tags">
                  <CategoryBadge value={selected.category} />
                  <RoleBadge value={selected.email_role} />
                  {selected.tender_id ? (
                    <button className="btn btn--small" onClick={() => onOpenTender(selected.tender_id)}>
                      Open tender {selected.tender_id}
                    </button>
                  ) : (
                    <span className="muted small">No tender id</span>
                  )}
                </div>

                {(selected.analysis_deleted || restoring || restoreError || restoreMessage) && (
                  <div className="restore-thread">
                    {selected.analysis_deleted && <>
                      <p className="muted small">This thread’s tender analysis was deleted. Restoring it will reanalyse incoming messages, extract offers, and process PDF attachments.</p>
                      <button className="btn btn--small btn--primary" disabled={restoring || refreshing}
                              onClick={handleRestoreThread}>
                        {restoring ? "Restoring and processing…" : "Restore & reprocess thread"}
                      </button>
                    </>}
                    {restoreError && <div className="notice notice--error">{restoreError}</div>}
                    {restoreMessage && <div className="notice notice--success">{restoreMessage}</div>}
                    {restoring && <p className="muted small">This can take a few minutes because email analysis and offer extraction call the LLM.</p>}
                  </div>
                )}

                {selected.thread_id && (
                  <div className="row">
                    <button className="btn btn--small" onClick={() => setShowThread((v) => !v)}>
                      {showThread ? "Hide conversation" : "Show conversation"}
                    </button>
                  </div>
                )}

                {showThread ? (
                  <ThreadView threadId={selected.thread_id} />
                ) : (
                  <EmailBody text={selected.body || selected.snippet} />
                )}
              </>
            )}
          </section>
        </div>
      )}
    </div>
  );
}
