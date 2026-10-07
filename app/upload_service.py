"""Shared PDF ingestion, analysis and linking for uploads and Gmail attachments."""

import base64
import hashlib
import json
import time

from app.document_service import CALL_PAUSE_SECONDS, _normalize_ref, analyze_document_text
from app.gmail_service import (
    extract_attachment_metadata,
    extract_body,
    get_gmail_service,
    get_header,
    _strip_quoted,
)
from app.link_service import apply_plan, plan_links
from app.pdf_service import ingest_pdf
from app.tender_store import (
    add_link_review,
    assign_documents_to_tender,
    get_document_by_sha,
    get_emails_for_tender,
    mark_email_attachments_checked,
    save_document_analysis,
)

MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024

_LINK_MESSAGES = {
    "create": "new tender created from documents sharing a reference number",
    "attach": "added to an existing tender",
    "unchanged": "already in a tender",
    "conflict": "matches two different tenders: queued for review, nothing changed",
    "unlinked": "shares no reference number with any other document: left unlinked",
    "email_attachment": "attached to the tender of the email",
}


def process_pdf_document(
    data: bytes,
    file_name: str,
    *,
    source: str = "upload",
    email_id: str | None = None,
    gmail_attachment_id: str | None = None,
    target_tender_id: str | None = None,
) -> dict:
    """Ingest and analyse one PDF, then link it by references or to a known email tender."""
    try:
        ingest = ingest_pdf(
            data,
            file_name,
            source=source,
            email_id=email_id,
            gmail_attachment_id=gmail_attachment_id,
        )
    except ValueError:
        raise
    except Exception as e:  # pymupdf raises its own error types for broken files
        raise ValueError(f"Could not read this file as a PDF: {e}") from e

    doc_id = ingest["document_id"]
    doc = get_document_by_sha(hashlib.sha256(data).hexdigest())
    if doc is None:
        raise RuntimeError("PDF ingestion completed but its document record could not be loaded")

    analysis_status = "already_analyzed" if doc["document_type"] else "analyzed"
    if not doc["document_type"]:
        result = analyze_document_text(doc["file_name"], doc["page_count"], doc["full_text"])
        time.sleep(CALL_PAUSE_SECONDS)
        if result is None:
            analysis_status = "failed"
        else:
            refs = sorted({(r.ref_type.value, _normalize_ref(r.ref_value)) for r in result.references})
            save_document_analysis(
                doc_id,
                document_type=result.document_type.value,
                contains_json=json.dumps(result.contains),
                analysis_json=result.model_dump_json(),
                references=refs,
            )

    link_action = None
    review_queued = False
    if target_tender_id:
        current_tender = doc.get("tender_id")
        if current_tender and current_tender != target_tender_id:
            review_queued = add_link_review(
                review_type="conflict",
                source_kind="document",
                source_id=str(doc_id),
                tender_id=target_tender_id,
                other_tender_id=current_tender,
                reason=(f"PDF attachment {doc['file_name']} belongs to an email in {target_tender_id}, "
                        f"but is already linked to {current_tender}. Review before moving it."),
            )
            link_action = "conflict"
        elif current_tender:
            link_action = "unchanged"
        else:
            assign_documents_to_tender(target_tender_id, [doc_id], "email_attachment")
            link_action = "email_attachment"
    elif analysis_status != "failed":
        entry = next((e for e in plan_links() if any(m["id"] == doc_id for m in e["members"])), None)
        if entry is not None:
            apply_plan([entry])
            link_action = entry["action"]

    doc = get_document_by_sha(hashlib.sha256(data).hexdigest())
    analysis = json.loads(doc["analysis_json"]) if doc["analysis_json"] else None
    message = (
        "stored, but analysis failed (retry later with: uv run python -m app.document_service)"
        if analysis_status == "failed" else _LINK_MESSAGES.get(link_action, "")
    )
    return {
        "document_id": doc_id,
        "file_name": doc["file_name"],
        "is_new": ingest["is_new"],
        "extraction_status": doc["extraction_status"],
        "page_count": doc["page_count"],
        "analysis_status": analysis_status,
        "document_type": doc["document_type"],
        "contains": json.loads(doc["contains_json"] or "[]"),
        "references": analysis["references"] if analysis else [],
        "tender_id": doc["tender_id"],
        "link_action": link_action,
        "review_queued": review_queued,
        "message": message,
    }


def process_upload(data: bytes, file_name: str) -> dict:
    """Run the shared PDF pipeline for a user-uploaded document."""
    return process_pdf_document(data, file_name, source="upload")


def _decode_attachment(data: str) -> bytes:
    """Decode Gmail's base64url attachment payload, rejecting malformed input."""
    padded = data + "=" * (-len(data) % 4)
    return base64.b64decode(padded, altchars=b"-_", validate=True)


def process_tender_attachments(
    tender_id: str, gmail_service=None, unchecked_only: bool = False,
) -> dict:
    """Download linked email PDFs; optionally scan only emails not checked before."""
    from app.classify_service import analyze_email_with_attachment_context
    from app.tender_store import normalize_tender_id

    tid = normalize_tender_id(tender_id)
    emails = get_emails_for_tender(tid, unchecked_attachments_only=unchecked_only)
    results = {
        "tender_id": tid,
        "emails_checked": 0,
        "attachments_found": 0,
        "pdfs_processed": 0,
        "skipped_non_pdf": 0,
        "skipped_too_large": 0,
        "document_analysis_failed": 0,
        "failed": 0,
        "classification_updated": 0,
        "reviews_queued": 0,
        "items": [],
    }
    if not emails:
        return results
    service = gmail_service or get_gmail_service()

    for email in emails:
        email_id = email["email_id"]
        results["emails_checked"] += 1
        failed_before = results["failed"]
        analysis_failed_before = results["document_analysis_failed"]
        try:
            message = service.users().messages().get(
                userId="me", id=email_id, format="full"
            ).execute()
        except Exception as exc:
            results["failed"] += 1
            results["items"].append({"email_id": email_id, "status": "failed", "error": str(exc)})
            continue

        payload = message.get("payload") or {}
        metadata = extract_attachment_metadata(payload)
        results["attachments_found"] += len(metadata)
        attachment_context = []
        headers = payload.get("headers", [])
        subject = get_header(headers, "Subject") or email.get("subject") or ""
        sender = get_header(headers, "From") or email.get("sender") or ""
        body = _strip_quoted(extract_body(payload))

        for attachment in metadata:
            name = attachment["filename"]
            mime = attachment["mime_type"]
            is_pdf = name.lower().endswith(".pdf") or mime == "application/pdf"
            if not is_pdf:
                results["skipped_non_pdf"] += 1
                results["items"].append({
                    "email_id": email_id, "file_name": name, "mime_type": mime,
                    "status": "skipped", "reason": "not a PDF",
                })
                continue
            if attachment.get("size") and attachment["size"] > MAX_ATTACHMENT_BYTES:
                results["skipped_too_large"] += 1
                results["items"].append({
                    "email_id": email_id, "file_name": name, "mime_type": mime,
                    "status": "skipped", "reason": "larger than 25 MB",
                })
                continue
            if not attachment.get("attachment_id"):
                results["failed"] += 1
                results["items"].append({
                    "email_id": email_id, "file_name": name, "mime_type": mime,
                    "status": "failed", "error": "Gmail attachment id is missing",
                })
                continue

            try:
                downloaded = service.users().messages().attachments().get(
                    userId="me", messageId=email_id, id=attachment["attachment_id"]
                ).execute()
                encoded = downloaded.get("data") or ""
                # Reject obviously oversized base64 before allocating its decoded copy.
                if len(encoded) > ((MAX_ATTACHMENT_BYTES + 2) // 3) * 4 + 8:
                    raise ValueError("Attachment is larger than 25 MB")
                data = _decode_attachment(encoded)
                if len(data) > MAX_ATTACHMENT_BYTES:
                    results["skipped_too_large"] += 1
                    results["items"].append({
                        "email_id": email_id, "file_name": name, "mime_type": mime,
                        "status": "skipped", "reason": "larger than 25 MB",
                    })
                    continue
                if b"%PDF-" not in data[:1024]:
                    raise ValueError("Attachment content is not a valid PDF")

                item = process_pdf_document(
                    data,
                    name,
                    source="gmail",
                    email_id=email_id,
                    gmail_attachment_id=attachment["attachment_id"],
                    target_tender_id=tid,
                )
                results["pdfs_processed"] += 1
                results["reviews_queued"] += int(item["review_queued"])
                results["document_analysis_failed"] += int(item["analysis_status"] == "failed")
                results["items"].append({
                    "email_id": email_id,
                    "file_name": name,
                    "mime_type": mime,
                    "status": "processed",
                    **item,
                })
                stored = get_document_by_sha(hashlib.sha256(data).hexdigest())
                excerpt = (stored.get("full_text") or "")[:1500]
                attachment_context.append(
                    f"Filename: {name}\nDocument type: {stored.get('document_type') or 'unclassified'}\n"
                    f"Extracted text (first 1500 characters):\n{excerpt}"
                )
            except Exception as exc:
                results["failed"] += 1
                results["items"].append({
                    "email_id": email_id, "file_name": name, "mime_type": mime,
                    "status": "failed", "error": str(exc),
                })

        if attachment_context:
            context = "\n\n".join(attachment_context)
            context_hash = hashlib.sha256(context.encode("utf-8")).hexdigest()
            try:
                classification = analyze_email_with_attachment_context(
                    email_id=email_id,
                    thread_id=email.get("thread_id"),
                    subject=subject,
                    body=body,
                    sender=sender,
                    attachment_context=context,
                    context_hash=context_hash,
                )
                results["classification_updated"] += int(classification.get("updated", False))
                if classification.get("failed"):
                    results["failed"] += 1
                    results["items"].append({
                        "email_id": email_id, "status": "classification_failed",
                    })
            except Exception as exc:
                results["failed"] += 1
                results["items"].append({
                    "email_id": email_id, "status": "classification_failed", "error": str(exc),
                })

        if results["failed"] == failed_before and results["document_analysis_failed"] == analysis_failed_before:
            mark_email_attachments_checked(email_id)

    return results
