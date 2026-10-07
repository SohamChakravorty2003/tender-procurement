import { useState } from "react";
import { sendEmail } from "../api";

export default function ComposeView() {
  const [recipient, setRecipient] = useState("");
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");
  const [sending, setSending] = useState(false);
  const [result, setResult] = useState(null); // { ok, text }

  async function submit(e) {
    e.preventDefault();
    setSending(true);
    setResult(null);
    try {
      await sendEmail({ recipient: recipient.trim(), subject, body });
      setResult({ ok: true, text: `Sent to ${recipient.trim()}.` });
      setSubject("");
      setBody("");
    } catch (err) {
      setResult({ ok: false, text: err.message });
    } finally {
      setSending(false);
    }
  }

  return (
    <div className="page page--narrow">
      <header className="page__head">
        <h1>New message</h1>
      </header>
      <form className="card form" onSubmit={submit}>
        <label>
          <span>To</span>
          <input className="input" type="email" required value={recipient} onChange={(e) => setRecipient(e.target.value)} />
        </label>
        <label>
          <span>Subject</span>
          <input className="input" required value={subject} onChange={(e) => setSubject(e.target.value)} />
        </label>
        <label>
          <span>Message</span>
          <textarea className="input" rows={10} required value={body} onChange={(e) => setBody(e.target.value)} />
        </label>
        <div className="form__actions">
          <button className="btn btn--primary" disabled={sending}>{sending ? "Sending…" : "Send"}</button>
          {result && <span className={result.ok ? "ok" : "err"}>{result.text}</span>}
        </div>
      </form>
    </div>
  );
}
