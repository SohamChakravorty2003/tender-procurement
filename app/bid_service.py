import json
import time
from email.utils import formataddr, parseaddr

from app.models import BidStatus, EmailRole, ThreadOffer
from app.tender_store import (
    get_bids_to_extract,
    get_unregistered_vendor_bid_emails,
    insert_bid,
    save_bid_extraction,
    save_email_offer_bids,
)


def register_missing_tender_bids(tender_id: str) -> dict:
    """Fetch only linked vendor-bid emails missing a bid row and register them.

    Cached classifications are reused; no inbox listing or LLM reclassification
    occurs. An unavailable Gmail message remains eligible for a later retry.
    """
    from app.gmail_service import get_email_by_id_for_offers

    counts = {"registered": 0, "failed": 0, "errors": []}
    for candidate in get_unregistered_vendor_bid_emails(tender_id):
        email_id = candidate["email_id"]
        try:
            if candidate["existing_bid_tender_id"]:
                raise ValueError(
                    f"offer already belongs to tender {candidate['existing_bid_tender_id']}; review the tender link"
                )
            email, attachment_names = get_email_by_id_for_offers(email_id)
            if candidate["thread_id"] and email.get("thread_id") != candidate["thread_id"]:
                raise ValueError("Gmail thread does not match the saved classification")
            if not email.get("sender") or not email.get("analysis_body"):
                raise ValueError("Gmail message has no sender or readable body")
            email["email_role"] = EmailRole.VENDOR_BID
            email["tender_id"] = candidate["tender_id"]
            counts["registered"] += int(register_bid_if_applicable(email, attachment_names))
        except Exception as exc:
            counts["failed"] += 1
            status = getattr(getattr(exc, "resp", None), "status", None)
            reason = f"Gmail request failed ({status})" if status else str(exc)
            counts["errors"].append(f"Email {email_id}: {reason}")
    return counts


def register_bid_if_applicable(email: dict, attachment_names: list[str] | None = None) -> bool:
    """
    Phase 1: if this email is a vendor bid with a known Tender ID, save a
    'pending' bid row (including the body text, so Phase 2 doesn't need Gmail).
    No LLM call. Safe to call repeatedly: an already-registered email is ignored.
    Returns True only if a new row was created.
    """
    if email.get("email_role") != EmailRole.VENDOR_BID:
        return False
    if not email.get("tender_id"):
        return False

    display_name, address = parseaddr(email["sender"])

    return insert_bid(
        tender_id=email["tender_id"],
        email_id=email["id"],
        offer_seq=1,
        vendor_name=display_name or None,
        vendor_email=address.lower() or None,
        offered_at=email.get("date"),
        body_text=email.get("analysis_body", email["body"]),
        attachment_names=json.dumps(attachment_names) if attachment_names else None,
    )


def extract_pending_bids(tender_id: str) -> dict:
    """
    Phase 2: extract every pending/failed bid for one tender.
    Returns counts, e.g. {"extracted": 2, "failed": 0}.
    """
    from app.offer_service import CALL_PAUSE_SECONDS, extract_message_offers

    counts = {"extracted": 0, "failed": 0}
    grouped: dict[str, dict] = {}
    for bid in get_bids_to_extract(tender_id):
        if bid.get("email_id"):
            grouped.setdefault(bid["email_id"], bid)

    for email_id, bid in grouped.items():
        sender = formataddr((bid.get("vendor_name") or "", bid.get("vendor_email") or ""))
        offers = extract_message_offers(sender, bid.get("body_text") or "")
        time.sleep(CALL_PAUSE_SECONDS)

        if offers is None:
            save_bid_extraction(bid["id"], status=BidStatus.FAILED.value)
            counts["failed"] += 1
            continue

        if not offers:
            offers = [ThreadOffer(is_bid=False)]

        records = []
        for seq, offer in enumerate(offers, start=1):
            # Keep the original Gmail From identity and Date header as source data.
            offer.sender = sender
            offer.offered_at = bid.get("offered_at")
            records.append({
                "offer_seq": seq,
                "offered_at": bid.get("offered_at"),
                "vendor_name": offer.vendor_company or bid.get("vendor_name"),
                "body_text": bid.get("body_text"),
                "extracted_json": offer.model_dump_json(),
            })
        save_email_offer_bids(email_id, records)
        counts["extracted"] += 1

    return counts
