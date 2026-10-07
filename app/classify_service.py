import os
import json
import time
from dotenv import load_dotenv
from langchain_groq import ChatGroq
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from app.models import EmailAnalysisResult
from app.tender_store import get_email_analysis, get_tender_id_for_thread, save_email_analysis

load_dotenv()

llm = ChatGroq(
    model="openai/gpt-oss-20b",
    temperature=0,
    api_key=os.getenv("GROQ_API_KEY"),
)

MAX_BODY_CHARS = 2000


analysis_prompt = ChatPromptTemplate.from_template(
    """You are analyzing one email from the procurement files of MinChem (Minchem Impex),
a trader that sits between customers and suppliers. Customers send MinChem enquiries,
MinChem forwards them to suppliers, suppliers quote, MinChem negotiates and confirms.
Senders at minchem.in (or "Minchem") are MinChem staff.

1. category: exactly one of
- quotation_request: asking for, offering or negotiating a PRICE for goods before an order is
  placed: enquiries / RFQs (from a customer or from MinChem to a supplier), supplier offers
  (including "bottom" or "lowest" price offers and offers that suggest a different grade),
  counter-offers, and a supplier accepting or refusing a price (even in one short line).
- follow_up: clarifications, reminders, or answers to questions (quantities, sizes, details)
  that neither request nor give a price.
- order_confirmation: an order or PO is placed or confirmed, or a contract is requested, sent
  for signing, or returned signed because of an order (including a supplier sending its sales
  contract after the customer's order).
- shipment_update: shipping status, shipping instructions, ETD/ETA, bill of lading, shipping documents.
- document_collection: documents are sent or requested as the main purpose (certificates, B/L
  copies, invoices) and there is no order, contract or price discussion.
- issue_resolution: complaints, claims, quality or delivery problems.
- other: anything else.
Tie-breaks: while the order is not yet confirmed, a message about price is quotation_request
whoever sends it. A message that confirms an order is order_confirmation even if it lists prices.
The category describes the topic, not the sender or direction.

2. email_role: exactly one of
- tender_issue: a request for a quotation / an enquiry, from ANY sender: a customer's enquiry,
  or MinChem asking a supplier to quote, forwarding an enquiry, or asking for a new or final
  price (including a new round with changed quantity). It is NOT tender_issue if MinChem proposes
  a specific price for the supplier to accept, or if a supplier only asks for details.
- vendor_bid: a SUPPLIER giving its own price offer (first, revised, "bottom" or "lowest" price,
  even with suggestions about grade), or a supplier accepting a price MinChem proposed.
- other: everything else, including MinChem's counter-offers ("please accept USD X"), a supplier
  asking for details before it prices, a supplier refusing a price without offering one,
  clarifications, order confirmations, and document emails.

3. tender_id: only an explicit identifier labeled as such ("Tender ID", "Tender Ref", "RFQ No",
or an enquiry code in brackets in the subject such as (DOMSE/723/PASSE)). Do NOT use product or
grade names (RKCB 86), quantities, customer names, or contract / PO numbers (including a
contract number cited as an earlier deal). If there is no explicit identifier, use null.

Sender: {sender}
Subject: {subject}
Body: {body}
Processed PDF attachment evidence: {attachment_context}
Use attachment evidence to interpret the category and role, but never take a tender id from it.

Respond with ONLY valid JSON, no other text, in exactly this shape:
{{"category": "...", "email_role": "...", "tender_id": "..." or null}}"""
)

analysis_chain = analysis_prompt | llm | StrOutputParser()


def _parse_llm_json(raw: str) -> dict:
    """
    LLMs frequently wrap JSON in markdown fences (```json ... ```) even when
    told not to. Strip that before parsing so a cosmetic wrapper doesn't
    count as a real failure.
    """
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned.removeprefix("json").strip()
    return json.loads(cleaned)


def analyze_email(
    subject: str,
    body: str,
    sender: str | None = None,
    attachment_context: str | None = None,
) -> EmailAnalysisResult | None:
    """
    Calls the LLM once and returns a validated EmailAnalysisResult, or None
    if the call failed, the response wasn't valid JSON, or the JSON didn't
    match the expected shape (bad enum value, missing field, etc). Returning
    None rather than a guessed default is what lets the caller mark the
    email as "failed" instead of silently caching a wrong answer as if it
    were correct.
    """
    truncated_body = body[:MAX_BODY_CHARS]

    try:
        raw = analysis_chain.invoke({
            "subject": subject,
            "body": truncated_body,
            "sender": sender or "unknown",
            "attachment_context": (attachment_context or "No processed PDF attachment evidence.")[:6000],
        })
        data = _parse_llm_json(raw)
        return EmailAnalysisResult.model_validate(data)

    except Exception as e:
        print(f"Email analysis failed: {e}")
        return None


def analyze_email_with_attachment_context(
    *,
    email_id: str,
    thread_id: str | None,
    subject: str,
    body: str,
    sender: str | None,
    attachment_context: str,
    context_hash: str,
) -> dict:
    """Reclassify one email when its processed PDF evidence changes; keep its tender assignment stable."""
    cached = get_email_analysis(email_id)
    if cached and cached.get("attachment_context_hash") == context_hash:
        return {"updated": False, "analysis": cached}

    result = analyze_email(subject, body, sender, attachment_context)
    if result is None:
        return {"updated": False, "failed": True, "analysis": cached}

    existing_tender = cached.get("tender_id") if cached else None
    inherited_tender = get_tender_id_for_thread(thread_id) if not existing_tender else None
    tender_id = existing_tender or inherited_tender or result.tender_id
    tender_id_source = (
        cached.get("tender_id_source") if existing_tender and cached else
        ("thread" if inherited_tender else ("llm" if result.tender_id else None))
    )
    save_email_analysis(
        email_id,
        thread_id=thread_id,
        sender=sender,
        subject=subject,
        category=result.category.value,
        email_role=result.email_role.value,
        tender_id=tender_id,
        tender_id_source=tender_id_source,
        status="ok",
        attachment_context_hash=context_hash,
    )
    time.sleep(2)
    return {"updated": True, "analysis": get_email_analysis(email_id)}


def analyze_email_cached(email_id: str, thread_id: str, subject: str, body: str, sender: str | None = None) -> dict:    
    """
    The cached, database-backed entry point gmail_service calls for every
    email. Order of operations:

      1. Already analyzed successfully? Return the cached row, no LLM call.
      2. Does this email's thread already have a known tender_id? If so,
         that's the Tender ID we use — free, and more reliable than asking
         the LLM to re-find it in a reply that may not repeat it.
      3. Ask the LLM for category + email_role + tender_id in one call.
      4. On success: the thread-inherited tender_id wins over the LLM's own
         guess if both exist. Save with status="ok".
         On failure: save with status="failed" and nothing else guessed.
      5. Sleep briefly after a *fresh* LLM call only (never on a cache hit),
         to stay well under Groq's rate limit.
      6. Re-read the row from the database before returning, so the caller
         always gets back exactly what's stored — one source of truth.
    """
    cached = get_email_analysis(email_id)
    if cached is not None and cached["status"] == "ok":
        if not cached.get("tender_id"):
            inherited_tender_id = get_tender_id_for_thread(thread_id)
            if inherited_tender_id:
                save_email_analysis(
                    email_id,
                    thread_id=thread_id,
                    sender=sender,
                    subject=subject,
                    category=cached["category"],
                    email_role=cached["email_role"],
                    tender_id=inherited_tender_id,
                    tender_id_source="thread",
                    confidence=cached.get("confidence"),
                    status="ok",
                )
                return get_email_analysis(email_id)
        return cached

    inherited_tender_id = get_tender_id_for_thread(thread_id)
    result = analyze_email(subject, body, sender)
    
    if result is None:
        save_email_analysis(
            email_id,
            thread_id=thread_id,
            sender = sender,
            subject=subject,
            status="failed",
        )
        return get_email_analysis(email_id)

    final_tender_id = inherited_tender_id or result.tender_id
    tender_id_source = (
        "thread" if inherited_tender_id
        else ("llm" if result.tender_id else None)
    )

    save_email_analysis(
        email_id,
        thread_id=thread_id,
        subject=subject,
        sender = sender,
        category=result.category.value,
        email_role=result.email_role.value,
        tender_id=final_tender_id,
        tender_id_source=tender_id_source,
        status="ok",
    )

    time.sleep(2)

    return get_email_analysis(email_id)


if __name__ == "__main__":
    result1 = analyze_email_cached(
        "proc001", "thread001",
        "RFQ: Caustic soda — Tender Ref TDR-2025-0142",
        "We are inviting quotations for supply of caustic soda under tender TDR-2025-0142. Deadline: 15 days."
    )
    print("Tender issue:", result1)

    result2 = analyze_email_cached(
        "proc002", "thread001",
        "Re: RFQ: Caustic soda",
        "Please find our quotation attached. We can deliver within 20 days."
    )
    print("Vendor bid, inherited tender_id:", result2)

    result3 = analyze_email_cached(
        "proc001", "thread001",
        "RFQ: Caustic soda — Tender Ref TDR-2025-0142",
        "We are inviting quotations for supply of caustic soda under tender TDR-2025-0142. Deadline: 15 days."
    )
    print("Cache hit (should be instant, same as result1):", result3)
