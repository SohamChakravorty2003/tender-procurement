"""Restore and reprocess one Gmail thread after deleting its tender."""

from app.bid_service import extract_pending_bids, register_bid_if_applicable
from app.classify_service import analyze_email_cached
from app.gmail_service import get_thread
from app.tender_store import (
    get_email_analysis,
    get_offer_documents,
    normalize_tender_id,
    prepare_deleted_thread_restore,
    save_email_analysis,
    save_offer_marks,
)


def restore_and_reprocess_thread(thread_id: str, anchor_email_id: str) -> dict:
    """Reanalyse an explicitly restored thread, extract its offers and PDFs, and refresh marks."""
    messages = get_thread(thread_id)
    email_ids = [message["id"] for message in messages if message.get("id")]
    restored = prepare_deleted_thread_restore(thread_id, email_ids, anchor_email_id)
    if restored is None:
        raise LookupError("This message is not a deleted tender message in the selected thread")

    tender_id = normalize_tender_id(restored.get("tender_id"))
    analysed = 0
    failed = 0
    for message in messages:
        if message.get("sent_by_me") or not message.get("id"):
            continue
        email_id = message["id"]
        analysis = analyze_email_cached(
            email_id=email_id,
            thread_id=thread_id,
            subject=message.get("subject") or "",
            body=message.get("body") or "",
            sender=message.get("sender"),
        )
        analysed += 1
        if analysis.get("status") == "failed":
            failed += 1
        if not tender_id:
            tender_id = normalize_tender_id(analysis.get("tender_id"))
        if tender_id and analysis.get("tender_id") != tender_id:
            save_email_analysis(
                email_id,
                thread_id=thread_id,
                sender=message.get("sender"),
                subject=message.get("subject"),
                category=analysis.get("category"),
                email_role=analysis.get("email_role"),
                tender_id=tender_id,
                tender_id_source="restored_thread",
                confidence=analysis.get("confidence"),
                status=analysis.get("status") or "ok",
            )
            analysis = get_email_analysis(email_id) or analysis
        register_bid_if_applicable({
            "id": email_id,
            "thread_id": thread_id,
            "sender": message.get("sender") or "",
            "date": message.get("date"),
            "body": message.get("body") or "",
            "analysis_body": message.get("body") or "",
            "email_role": analysis.get("email_role"),
            "tender_id": analysis.get("tender_id"),
        }, message.get("attachments") or [])

    result = {
        "tender_id": tender_id,
        "messages_analysed": analysed,
        "analysis_failed": failed,
        "offer_emails_extracted": 0,
        "offer_emails_failed": 0,
        "pdfs_processed": 0,
        "pdfs_failed": 0,
        "thread_offers_saved": 0,
        "duplicate_offers_marked": 0,
    }
    if not tender_id:
        return result

    offer_counts = extract_pending_bids(tender_id)
    result["offer_emails_extracted"] = offer_counts["extracted"]
    result["offer_emails_failed"] = offer_counts["failed"]

    from app.upload_service import process_tender_attachments
    attachment_counts = process_tender_attachments(tender_id)
    result["pdfs_processed"] = attachment_counts["pdfs_processed"]
    result["pdfs_failed"] = attachment_counts["failed"] + attachment_counts["document_analysis_failed"]

    from app.offer_service import extract_thread_offers
    offer_docs = [doc for doc in get_offer_documents() if doc.get("tender_id") == tender_id]
    if offer_docs:
        doc_counts = extract_thread_offers(ids={doc["id"] for doc in offer_docs})
        result["thread_offers_saved"] = doc_counts["offers_saved"]

    from app.accept_service import plan_tender
    plan = plan_tender(tender_id)
    save_offer_marks(tender_id, plan["duplicates"], plan["accepted_ids"])
    result["duplicate_offers_marked"] = len(plan["duplicates"])
    return result
