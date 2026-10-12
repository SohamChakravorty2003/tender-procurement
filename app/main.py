import hashlib
from urllib.parse import quote

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool

from app.models import (
    EmailRequest,
    TenderOfferExtractionRequest,
    TenderAttachmentProcessingRequest,
    TenderDeletionPreviewRequest,
    TenderDeletionRequest,
    DeletedThreadRestoreRequest,
    V2ClusterAssignmentRequest,
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
    create_v2_document_job,
    assign_v2_review_cluster,
    get_v2_document,
    get_v2_document_pdf,
    get_v2_job,
    list_v2_documents,
    list_v2_reviews,
    reopen_v2_review,
    resolve_v2_review,
    retry_v2_job,
    seal_v2_upload_batch,
    sync_v2_email_messages,
)
from app.upload_service import process_tender_attachments
from app.bid_service import extract_pending_bids, register_missing_tender_bids
from app.offer_service import extract_thread_offers
from app.thread_restore_service import restore_and_reprocess_thread
from app.v2_job_service import process_v2_document_job

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

    sync_v2_email_messages(emails)
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
        "duplicates_marked": 0,
        "accepted_marked": 0,
        "accepted_matching": "disabled; V2 contract reconciliation requires review",
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


@app.post("/documents/upload", status_code=202)
async def upload_document(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    upload_batch_id: str | None = Form(default=None),
):
    """Queue the durable V2 image-analysis workflow for one PDF upload."""
    name = file.filename or "upload.pdf"
    if not name.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="The file is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File is larger than 25 MB")
    if upload_batch_id is not None and (not upload_batch_id.strip() or len(upload_batch_id) > 100):
        raise HTTPException(status_code=400, detail="upload_batch_id must be between 1 and 100 characters")
    queued = create_v2_document_job(
        name,
        data,
        hashlib.sha256(data).hexdigest(),
        upload_batch_id=upload_batch_id.strip() if upload_batch_id else None,
    )
    if queued["status"] == "queued" and queued["is_new"]:
        background_tasks.add_task(process_v2_document_job, queued["job_id"])
    return queued


@app.get("/documents")
def read_v2_documents():
    """List durable V2 documents and their processing state."""
    return {"documents": list_v2_documents()}


@app.post("/documents/upload-batches/{upload_batch_id}/complete")
def complete_upload_batch(upload_batch_id: str):
    """Seal a browser selection so its completed PDFs can be batch-reconciled."""
    if not upload_batch_id.strip() or len(upload_batch_id) > 100:
        raise HTTPException(status_code=400, detail="upload_batch_id must be between 1 and 100 characters")
    return seal_v2_upload_batch(upload_batch_id.strip())


@app.get("/documents/{document_id}/pages")
def read_v2_document_pages(document_id: int):
    document = get_v2_document(document_id)
    if document is None:
        raise HTTPException(status_code=404, detail=f"Document {document_id} not found")
    return {"document_id": document_id, "pages": document["pages"]}


@app.get("/documents/{document_id}/file")
def read_v2_document_pdf(document_id: int):
    """Serve the locally stored original PDF for an on-demand review preview."""
    document = get_v2_document_pdf(document_id)
    if document is None:
        raise HTTPException(status_code=404, detail=f"Document {document_id} not found")
    file_name, pdf_bytes = document
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f"inline; filename*=UTF-8''{quote(file_name)}"},
    )


@app.get("/documents/{document_id}")
def read_v2_document(document_id: int):
    document = get_v2_document(document_id)
    if document is None:
        raise HTTPException(status_code=404, detail=f"Document {document_id} not found")
    return document


@app.get("/processing-jobs/{job_id}")
def read_v2_processing_job(job_id: int):
    job = get_v2_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Processing job {job_id} not found")
    return job


@app.post("/processing-jobs/{job_id}/retry", status_code=202)
def retry_v2_processing_job(job_id: int, background_tasks: BackgroundTasks):
    try:
        job = retry_v2_job(job_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    background_tasks.add_task(process_v2_document_job, job_id)
    return job


@app.get("/v2/reviews")
def read_v2_reviews(status: str | None = "pending"):
    return {"reviews": list_v2_reviews(None if status == "all" else status)}


@app.post("/v2/reviews/{review_id}/resolve")
def resolve_v2_document_review(review_id: int, body: ReviewResolution):
    """Apply a human tender assignment to one image-first PDF review."""
    try:
        return resolve_v2_review(review_id, action=body.action, tender_id=body.tender_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.post("/v2/reviews/{review_id}/assign-cluster")
def assign_v2_document_cluster(review_id: int, body: V2ClusterAssignmentRequest):
    """Apply one human decision to the review's persisted reference component."""
    try:
        return assign_v2_review_cluster(review_id, body.tender_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.post("/v2/reviews/{review_id}/reopen")
def reopen_v2_document_review(review_id: int):
    """Return a deliberately ungrouped PDF to the document-review queue."""
    try:
        return reopen_v2_review(review_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


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
