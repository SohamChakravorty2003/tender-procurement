import { useEffect, useState } from "react";
import { completeUploadBatch, getProcessingJob, retryProcessingJob, uploadPdf } from "../api";
import { Badge } from "./Badges";
import { prettyWord } from "../constants";

export default function UploadBox({ onDone }) {
  const [busy, setBusy] = useState(false);
  const [drag, setDrag] = useState(false);
  const [results, setResults] = useState([]);
  const [error, setError] = useState(null);
  const [retryingJobId, setRetryingJobId] = useState(null);

  useEffect(() => {
    const active = results.some((result) => ["queued", "running"].includes(result.status));
    if (!active) return undefined;
    let cancelled = false;

    async function refreshStatuses() {
      try {
        const latest = await Promise.all(results.map(async (result) => ({
          ...result,
          ...(await getProcessingJob(result.job_id))
        })));
        if (cancelled) return;
        const changed = latest.some((result, index) => (
          ["status", "stage", "current_page", "total_pages", "error_text"].some(
            (key) => result[key] !== results[index][key]
          )
        ));
        if (changed) {
          setResults(latest);
          onDone?.();
        }
      } catch {
        // A temporary status-poll failure should not hide the submitted PDFs.
      }
    }

    refreshStatuses();
    const timer = setInterval(refreshStatuses, 3000);
    return () => { cancelled = true; clearInterval(timer); };
  }, [onDone, results]);

  useEffect(() => {
    const complete = !busy && results.length > 0 && results.every(
      (result) => ["completed", "failed"].includes(result.status)
    );
    if (complete) onDone?.();
  }, [busy, onDone, results]);

  async function send(files) {
    const pdfs = Array.from(files || []).filter((file) => file.name.toLowerCase().endsWith(".pdf"));
    if (!pdfs.length) return;
    setBusy(true);
    setError(null);
    setResults([]);
    const uploadBatchId = globalThis.crypto?.randomUUID?.() ||
      `upload-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    const queued = [];
    const failures = [];
    for (const file of pdfs) {
      try {
        queued.push({ fileName: file.name, ...(await uploadPdf(file, uploadBatchId)) });
        onDone?.();
      } catch (e) {
        failures.push(`${file.name}: ${e.message}`);
      }
    }
    try {
      await completeUploadBatch(uploadBatchId);
    } catch (e) {
      failures.push(`Could not finalise this upload set: ${e.message}`);
    }
    setResults(queued);
    setError(failures.length ? failures.join(" ") : null);
    setBusy(false);
  }

  async function retry(result) {
    setRetryingJobId(result.job_id);
    setError(null);
    try {
      const job = await retryProcessingJob(result.job_id);
      setResults((current) => current.map((item) => (
        item.job_id === result.job_id ? { ...item, ...job } : item
      )));
    } catch (e) {
      setError(`${result.fileName}: ${e.message}`);
    } finally {
      setRetryingJobId(null);
    }
  }

  const activeCount = results.filter((result) => ["queued", "running"].includes(result.status)).length;
  const completedCount = results.filter((result) => result.status === "completed").length;
  const failedCount = results.filter((result) => result.status === "failed").length;

  function statusTone(status) {
    if (status === "completed") return "green";
    if (status === "failed") return "red";
    return status === "running" ? "blue" : "amber";
  }

  function stageLabel(stage) {
    const labels = {
      rendering: "Rendering pages",
      routing: "Reading page layout",
      classifying: "VLM classification",
      extracting: "Structured extraction",
      persisting: "Saving results",
      grouping: "Reference grouping"
    };
    return labels[stage] || prettyWord(stage);
  }

  return <div className="card">
    <label className={`drop ${drag ? "is-drag" : ""} ${busy ? "is-busy" : ""}`}
      onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
      onDragLeave={() => setDrag(false)}
      onDrop={(e) => { e.preventDefault(); setDrag(false); send(e.dataTransfer.files); }}>
      <input type="file" accept="application/pdf" multiple hidden disabled={busy}
        onChange={(e) => { send(e.target.files); e.target.value = ""; }} />
      {busy ? "Queueing PDFs for image analysis..." : "Drop one or more PDFs here, or click to choose them"}
    </label>
    <p className="muted small">Files are grouped only by explicit tender IDs and matching typed references. A shared upload selection is never grouping evidence by itself.</p>
    {error && <div className="notice notice--error">{error}</div>}
    {results.length > 0 && <div className="result">
      <strong>
        {results.length} PDF{results.length === 1 ? "" : "s"} submitted
        {activeCount > 0 && ` · ${activeCount} processing`}
        {completedCount > 0 && ` · ${completedCount} completed`}
        {failedCount > 0 && ` · ${failedCount} failed`}
      </strong>
      {results.map((result) => <div className="row" key={`${result.document_id}-${result.fileName}`}>
        <span>{result.fileName}</span>
        <Badge tone={statusTone(result.status)}>{prettyWord(result.status)}</Badge>
        {result.stage && !["queued", "completed", "failed"].includes(result.stage) && <span className="muted small">{stageLabel(result.stage)}{result.total_pages ? ` · ${result.current_page || 0}/${result.total_pages} pages` : ""}</span>}
        <span className="muted small">document #{result.document_id}, job #{result.job_id}</span>
        {result.status === "failed" && result.error_text && <span className="notice notice--error small">{result.error_text}</span>}
        {result.status === "failed" && (
          <button className="btn btn--small" type="button" disabled={retryingJobId === result.job_id}
                  onClick={() => retry(result)}>
            {retryingJobId === result.job_id ? "Retrying…" : "Retry"}
          </button>
        )}
      </div>)}
    </div>}
  </div>;
}
