const BASE = import.meta.env.VITE_API_URL || "http://localhost:8000";

async function request(path, options) {
  let res;
  try {
    res = await fetch(`${BASE}${path}`, options);
  } catch {
    throw new Error("Can't reach the backend. Is it running on " + BASE + "?");
  }
  if (!res.ok) {
    let message = `Request failed (${res.status})`;
    try {
      const data = await res.json();
      if (typeof data?.detail === "string") message = data.detail;
    } catch {
      // body was not JSON: keep the generic message
    }
    throw new Error(message);
  }
  return res.json();
}

export const getEmails = () => request("/emails").then((d) => d.emails ?? []);

export const sendEmail = ({ recipient, subject, body }) =>
  request("/send-email", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ recipient, subject, body })
  });

export const getTenders = () => request("/tenders").then((d) => d.tenders ?? []);

export const previewTenderDeletion = (tenderId) =>
  request("/tenders/delete-preview", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tender_id: tenderId })
  });

export const deleteTender = (tenderId, confirmTenderId) =>
  request("/tenders/delete", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tender_id: tenderId, confirm_tender_id: confirmTenderId })
  });

// Tender ids can contain slashes (e.g. DOMSE/723/PASSE); the backend route
// uses a path converter, so encodeURI (which keeps "/") is correct here.
export const getTender = (id) => request(`/tenders/${encodeURI(id)}`);

export const extractTenderOffers = (tenderId) =>
  request("/tenders/extract-offers", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tender_id: tenderId })
  });

export const processTenderAttachments = (tenderId) =>
  request("/tenders/process-attachments", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tender_id: tenderId })
  });

export function uploadPdf(file) {
  const form = new FormData();
  form.append("file", file);
  return request("/documents/upload", { method: "POST", body: form });
}

export const getReviews = (status = "pending") =>
  request(`/reviews?status=${status}`).then((d) => d.reviews ?? []);

export const resolveReview = (id, action, tenderId) =>
  request(`/reviews/${id}/resolve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, tender_id: tenderId ?? null })
  });

export const getThread = (threadId) =>
  request(`/threads/${encodeURIComponent(threadId)}`).then((d) => d.messages ?? []);

export const restoreDeletedThread = (emailId, threadId) =>
  request("/threads/restore-deleted", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email_id: emailId, thread_id: threadId })
  });
