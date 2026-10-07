from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool

from app.models import (
    EmailRequest,
    TenderOfferExtractionRequest,
    TenderAttachmentProcessingRequest,
    TenderDeletionPreviewRequest,
    TenderDeletionRequest,
    DeletedThreadRestoreRequest,
)
from app.models import EmailListResponse, ReviewResolution
from app.email_service import EmailService
from googleapiclient.errors import HttpError
from app.gmail_service import get_emails, get_thread
from app.tender_store import (
    delete_tender_data,
    get_tender_deletion_preview,
    get_tender_view,
    list_link_reviews,
    list_tenders,
    resolve_link_review,
    save_offer_marks,
)
from app.upload_service import process_upload, process_tender_attachments
from app.bid_service import extract_pending_bids, register_missing_tender_bids
from app.offer_service import extract_thread_offers
from app.accept_service import plan_tender
from app.thread_restore_service import restore_and_reprocess_thread

email_service = EmailService()
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

MAX_UPLOAD_BYTES = 25 * 1024 * 1024


@app.post("/send-email")
def send_email(email_request: EmailRequest):
    email_service.send_email(
        recipient=email_request.recipient,
        subject=email_request.subject,
        body=email_request.body
    )
    return {
        "message": "Email sent successfully!",
        "recipient": email_request.recipient,
        "subject": email_request.subject,
        "body": email_request.body
    }


@app.get("/emails", response_model=EmailListResponse)
def read_emails():
    try:
        emails = get_emails(max_results=50)
    except HttpError as e:
        status = getattr(e.resp, "status", 502)
        reason = str(e)
        if status == 403 and any(token in reason for token in ("rateLimitExceeded", "userRateLimitExceeded")):
            raise HTTPException(
                status_code=429,
                detail="Gmail is temporarily rate-limiting inbox refreshes. Wait about a minute, then retry.",
            ) from e
        raise HTTPException(status_code=502, detail=f"Gmail error: {e}") from e

    return {
        "emails": emails
    }

@app.get("/threads/{thread_id}")
def read_thread(thread_id: str):
    """The full Gmail conversation (both directions), oldest first."""
    try:
        return {"messages": get_thread(thread_id)}
    except HttpError as e:
        status = getattr(e.resp, "status", 502)
        if status == 404:
            raise HTTPException(status_code=404, detail=f"Thread {thread_id} not found")
        raise HTTPException(status_code=502, detail=f"Gmail error: {e}")


@app.post("/threads/restore-deleted")
async def restore_deleted_thread(body: DeletedThreadRestoreRequest):
    """Restore an inbox message's deleted thread, offers and PDF attachments."""
    try:
        return await run_in_threadpool(
            restore_and_reprocess_thread, body.thread_id, body.email_id
        )
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except HttpError as e:
        status = getattr(e.resp, "status", 502)
        if status == 404:
            raise HTTPException(status_code=404, detail=f"Gmail thread {body.thread_id} not found") from e
        raise HTTPException(status_code=502, detail=f"Gmail error: {e}") from e
    except SystemExit as e:
        raise HTTPException(status_code=429, detail=str(e)) from e


@app.get("/tenders")
def read_tenders():
    return {"tenders": list_tenders()}


@app.post("/tenders/delete-preview")
def preview_tender_deletion(body: TenderDeletionPreviewRequest):
    preview = get_tender_deletion_preview(body.tender_id)
    if preview is None:
        raise HTTPException(status_code=404, detail=f"Tender {body.tender_id} not found")
    return preview


@app.post("/tenders/delete")
def remove_tender(body: TenderDeletionRequest):
    if body.confirm_tender_id.strip() != body.tender_id.strip():
        raise HTTPException(status_code=400, detail="Confirmation must exactly match the tender ID")
    result = delete_tender_data(body.tender_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Tender {body.tender_id} not found")
    return result


@app.post("/tenders/extract-offers")
async def extract_tender_offers(body: TenderOfferExtractionRequest):
    """Find linked email/PDF offer sources, extract them, then refresh derived marks."""
    if get_tender_view(body.tender_id) is None:
        raise HTTPException(status_code=404, detail=f"Tender {body.tender_id} not found")
    try:
        attachment_counts = await run_in_threadpool(
            process_tender_attachments, body.tender_id, None, True
        )
        registration_counts = await run_in_threadpool(
            register_missing_tender_bids, body.tender_id
        )
        email_counts = await run_in_threadpool(extract_pending_bids, body.tender_id)
        document_counts = await run_in_threadpool(
            extract_thread_offers, False, None, False, body.tender_id
        )
    except SystemExit as e:
        raise HTTPException(status_code=429, detail=str(e)) from e
    plan = plan_tender(body.tender_id)
    save_offer_marks(body.tender_id, plan["duplicates"], plan["accepted_ids"])
    return {
        "tender_id": body.tender_id,
        "attachment_emails_checked": attachment_counts["emails_checked"],
        "attachment_pdfs_processed": attachment_counts["pdfs_processed"],
        "attachment_failed": attachment_counts["failed"] + attachment_counts["document_analysis_failed"],
        "attachment_reviews_queued": attachment_counts["reviews_queued"],
        "emails_registered": registration_counts["registered"],
        "email_registration_failed": registration_counts["failed"],
        "email_registration_errors": registration_counts["errors"],
        **email_counts,
        "thread_documents_processed": document_counts["documents"],
        "thread_offers_saved": document_counts["offers_saved"],
        "thread_documents_failed": document_counts["failed"],
        "thread_errors": document_counts["errors"],
        "thread_documents_without_offers": document_counts["no_offers"],
        "duplicates_marked": len(plan["duplicates"]),
        "accepted_marked": len(plan["accepted_ids"]),
    }


@app.post("/tenders/process-attachments")
async def process_tender_pdf_attachments(body: TenderAttachmentProcessingRequest):
    """Explicitly download, read and link PDF attachments from a tender's Gmail emails."""
    if get_tender_view(body.tender_id) is None:
        raise HTTPException(status_code=404, detail=f"Tender {body.tender_id} not found")
    try:
        return await run_in_threadpool(process_tender_attachments, body.tender_id)
    except HttpError as e:
        status = getattr(e.resp, "status", 502)
        if status == 404:
            raise HTTPException(status_code=404, detail=f"Gmail message not found: {e}")
        raise HTTPException(status_code=502, detail=f"Gmail error: {e}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not process Gmail attachments: {e}") from e


@app.post("/documents/upload")
async def upload_document(file: UploadFile = File(...)):
    name = file.filename or "upload.pdf"
    if not name.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="The file is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File is larger than 25 MB")
    try:
        return await run_in_threadpool(process_upload, data, name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/reviews")
def read_reviews(status: str | None = "pending"):
    """Link reviews waiting for a human decision. Use ?status=all for every review."""
    return {"reviews": list_link_reviews(None if status == "all" else status)}


@app.post("/reviews/{review_id}/resolve")
def resolve_review(review_id: int, body: ReviewResolution):
    try:
        return resolve_link_review(review_id, action=body.action, tender_id=body.tender_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# Keep this route last: the :path converter matches anything after /tenders/.
@app.get("/tenders/{tender_id:path}")
def read_tender(tender_id: str):
    view = get_tender_view(tender_id)
    if view is None:
        raise HTTPException(status_code=404, detail=f"Tender {tender_id} not found")
    return view
