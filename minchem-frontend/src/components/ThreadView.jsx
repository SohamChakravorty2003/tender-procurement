import { useEffect, useState } from "react";
import { getThread } from "../api";
import { Badge } from "./Badges";
import { senderName } from "../constants";

function when(value) {
  const d = new Date(value);
  return isNaN(d) ? value : d.toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

/** The whole Gmail conversation of one thread, both directions, oldest first. */
export default function ThreadView({ threadId, initialMessages }) {
  const [messages, setMessages] = useState(initialMessages ?? null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    if (initialMessages) {
      setMessages(initialMessages);
    } else {
      setMessages(null);
      getThread(threadId)
        .then((m) => !cancelled && setMessages(m))
        .catch((e) => !cancelled && setError(e.message));
    }
    return () => { cancelled = true; };
  }, [threadId, initialMessages]);

  if (error) return <div className="notice notice--error">{error}</div>;
  if (!messages) return <p className="muted">Loading conversation…</p>;
  if (messages.length === 0) return <p className="empty">No messages in this thread.</p>;

  return (
    <div className="thread">
      <div className="muted small">{messages.length} messages in this conversation</div>
      {messages.map((m) => (
        <div key={m.id} className={`bubble ${m.sent_by_me ? "bubble--me" : ""}`}>
          <div className="bubble__head">
            <strong>{senderName(m.sender)}</strong>
            {m.sent_by_me && <Badge tone="blue">MinChem</Badge>}
            <span className="muted small">{when(m.date)}</span>
          </div>
          <pre className="bubble__body">{m.body || "(no text)"}</pre>
          {m.attachments.length > 0 && (
            <div className="row">
              {m.attachments.map((n) => <Badge key={n} tone="purple" title="Attachment name only; the file is not read yet">{n}</Badge>)}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
