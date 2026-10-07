import { useState } from "react";
import { uploadPdf } from "../api";
import { Badge } from "./Badges";
import { docTypeLabel, prettyWord } from "../constants";

export default function UploadBox({ onDone, onOpenTender }) {
  const [busy, setBusy] = useState(false);
  const [drag, setDrag] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);

  async function send(file) {
    if (!file) return;
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      setResult(await uploadPdf(file));
      onDone?.();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card">
      <label
        className={`drop ${drag ? "is-drag" : ""} ${busy ? "is-busy" : ""}`}
        onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
        onDragLeave={() => setDrag(false)}
        onDrop={(e) => { e.preventDefault(); setDrag(false); send(e.dataTransfer.files[0]); }}
      >
        <input
          type="file"
          accept="application/pdf"
          hidden
          disabled={busy}
          onChange={(e) => { send(e.target.files[0]); e.target.value = ""; }}
        />
        {busy ? "Reading, analysing and linking the PDF…" : "Drop a PDF here, or click to choose one"}
      </label>

      {error && <div className="notice notice--error">{error}</div>}

      {result && (
        <div className="result">
          <div className="result__title">
            {result.file_name} {!result.is_new && <Badge>already stored</Badge>}
          </div>
          <div className="row">
            <Badge tone="blue">{result.document_type ? docTypeLabel(result.document_type) : "not analysed"}</Badge>
            <Badge tone={result.extraction_status === "ok" ? "green" : "amber"}>text {result.extraction_status}</Badge>
            <span className="muted small">{result.page_count} pages</span>
          </div>
          {result.contains?.length > 0 && (
            <div className="row">{result.contains.map((c) => <Badge key={c}>{prettyWord(c)}</Badge>)}</div>
          )}
          {result.references?.length > 0 && (
            <div className="muted small">
              References: {result.references.map((r) => `${prettyWord(r.ref_type)} ${r.ref_value}`).join(" · ")}
            </div>
          )}
          <div>
            Tender:{" "}
            {result.tender_id ? (
              <button className="chip" onClick={() => onOpenTender(result.tender_id)}>{result.tender_id}</button>
            ) : (
              <strong>none</strong>
            )}
            <span className="muted"> {result.message}</span>
          </div>
        </div>
      )}
    </div>
  );
}
