"""
Offer extraction: splits email-thread documents into messages (thread_split),
reads standalone quotation PDFs as one source, and saves supplier offers as
rows in `bids` (document_id + offer_seq + offered_at).

Stored: supplier offers, and a supplier accepting Minchem's counter-price
(kind = "vendor_acceptance").
Not stored: customer enquiries, Minchem messages, clarifications.
Sender and date come from the message header, never from the model.

    uv run python -m app.offer_service                 # linked offer-source PDFs without saved offers
    uv run python -m app.offer_service --ids 2 9 --dry # show what would be extracted, write nothing
    uv run python -m app.offer_service --ids 2 9       # extract only if these have no saved offers
    uv run python -m app.offer_service --redo --ids 2 9 # replace offers from these documents
    uv run python -m app.offer_service --redo          # re-extract all threads
Re-extracting replaces the document's old offers (only if every call succeeded).
"""

import argparse
import re
import time

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate

from app.classify_service import _parse_llm_json
from app.document_service import doc_llm, _safe
from app.models import ThreadOffer, ThreadOffers
from app.tender_store import (
    get_offer_documents, normalize_tender_id, replace_document_offers,
    set_thread_offer_extraction_status,
)
from app.thread_split import split_thread

MAX_MESSAGE_CHARS = 4000
MAX_QUOTATION_CHARS = 16000
CALL_PAUSE_SECONDS = 3

message_prompt = ChatPromptTemplate.from_template(
    """Below is ONE email message written by {sender}. Minchem Impex is a trader between
customers and suppliers; this message is from the other side. Decide what it is:

- "vendor_offer": the sender offers a price (including revised or "bottom price" offers).
- "vendor_acceptance": the sender accepts a price proposed by Minchem
  (for example "we accept USD875/MT"); put the accepted price in unit_price.
- Neither: a customer enquiry, a question, "please send details", "please find signed SC",
  a price rejection with no price offered, or anything without a supplier price. Return an
  empty list.

Rules:
- The message may end with quoted earlier messages (for example Minchem's counter-offer
  printed below the sender's reply). Use ONLY the sender's own new text at the top. A price
  that appears only in quoted text is not an offer by this sender: ignore it. Use
  "vendor_acceptance" only if the sender's own new text says they accept a price.
- If one supplier message lists the SAME unit price, currency, quantity, and price terms
  for several grades/sizes, group those grades into ONE offer entry and list them together
  in "specifications". If a message gives different price tiers, make one entry per tier.
- "quantity" is the stated order amount with its units (for example, "22 MT / 20 FCL").
  Grade labels such as +896 or +899 and density values such as 50 g/100ml are not quantities;
  put grade and product details in "specifications". Return quantity as text or null.
- For tiered tables, keep the row's grade and density together as plain text in
  "specifications" (for example, "Grade +896; density 50 g/100ml"). "products_offered"
  is a list of product names, not a JSON object. Do not return objects for text fields.
- Never guess: use null where something is not stated. Never calculate a value.
- Prices are plain numbers (no symbols, no commas); currency code in "currency".
  "$" or "usd" means USD. A number next to the word "mesh" (100mesh, 325mesh, 6/10 mesh)
  or "mm" is a size, never a price.
- If the message gives one price for the general grades and a different price for specific
  sizes (for example "FOB 660usd. 100mesh 325mesh 675usd"), make one entry for each: the
  general price with specifications null, and the specific price with those sizes in
  "specifications".
- Example: "USD 650 for 5-8mm, 3-5mm and 6/10 mesh; USD 665 for 30-60, 60-90 and
  -100 mesh" is TWO offer entries (650 and 665), not one entry per size. All grades in
  each entry must be retained in specifications.
- price_terms: the basis as written (FOB Xingang, CFR Chennai, ...).
- price_text: the sender's own wording about the price, copied as written.
- delivery_days: whole days only if a duration is given.
- validity: offer validity as written.

Message:
{text}

Respond with ONLY valid JSON, no other text, in exactly this shape:
{{"offers": [{{"kind": "vendor_offer", "vendor_company": null, "unit_price": null,
"total_price": null, "currency": null, "price_terms": null, "price_text": null,
"products_offered": null, "quantity": null, "specifications": null,
"delivery_days": null, "delivery_text": null, "payment_terms": null,
"warranty_support": null, "validity": null, "exceptions_or_deviations": null}}]}}"""
)

message_chain = message_prompt | doc_llm | StrOutputParser()

quotation_prompt = ChatPromptTemplate.from_template(
    """This is the extracted text of ONE standalone supplier quotation PDF.
Extract every supplier price tier stated in this document. A different unit
price, quantity, grade or price term is a separate offer. Do not use a customer
target price, a payment amount, an old quote mentioned for reference, or a
price that is not offered by the supplier. Do not invent missing fields.
Keep the exact stated price terms and quote wording. A number next to mesh,
mm, or a date is not a price. If no supplier offer is present, return [].
vendor_company must come from the PDF text, or be null.

PDF text:
{text}

Respond with ONLY valid JSON in this shape:
{{"offers": [{{"kind": "vendor_offer", "vendor_company": null,
"unit_price": null, "total_price": null, "currency": null,
"price_terms": null, "price_text": null, "products_offered": null,
"quantity": null, "specifications": null, "delivery_days": null,
"delivery_text": null, "payment_terms": null, "warranty_support": null,
"validity": null, "exceptions_or_deviations": null}}]}}"""
)
quotation_chain = quotation_prompt | doc_llm | StrOutputParser()

repair_prompt = ChatPromptTemplate.from_template(
    """Your previous extraction of this ONE supplier source could not be accepted:
{issue}

Read the source again. Return ALL distinct quoted price tiers, one
entry per price/quantity/terms combination. Do not invent facts or use quoted
earlier messages. If this message has no supplier offer, return an empty list.
Include every explicit currency-labelled price from the message, keeping the
quantity and price terms attached to the correct tier. Missing facts must be null.
"quantity" must be the stated order amount with its units (for example, "22 MT / 20 FCL"),
not a grade label such as +896 or a density such as 50 g/100ml. Return it as text or null.
For tiered tables, keep each row's grade and density together as plain text in
"specifications"; "products_offered" is a list of product names, not a JSON object.
Do not return objects for text fields.

Source: {sender}
Original source text:
{text}

Respond with ONLY valid JSON in this shape:
{{"offers": [{{"kind": "vendor_offer", "vendor_company": null,
"unit_price": null, "total_price": null, "currency": null,
"price_terms": null, "price_text": null, "products_offered": null,
"quantity": null, "specifications": null, "delivery_days": null,
"delivery_text": null, "payment_terms": null, "warranty_support": null,
"validity": null, "exceptions_or_deviations": null}}]}}"""
)
repair_chain = repair_prompt | doc_llm | StrOutputParser()

_CURRENCY_AMOUNT = re.compile(
    r"(?:\b(?:USD|US\s*\$|EUR|RMB|CNY|INR|GBP)\s*|[$€¥])"
    r"(\d[\d,]*(?:\.\d+)?)|"
    r"(\d[\d,]*(?:\.\d+)?)\s*(?:USD|EUR|RMB|CNY|INR|GBP)\b",
    re.I,
)
_REJECTED_PRICE_CONTEXT = re.compile(
    r"\b(?:too\s+(?:low|high)|not\s+acceptable|cannot\s+accept|can\s+not\s+accept|"
    r"can't\s+accept|can’t\s+accept|unable\s+to\s+accept|not\s+able\s+to\s+accept|"
    r"reject(?:ed)?|declin(?:e|ed))\b",
    re.I,
)
_QUANTITY_WITH_UNIT = re.compile(
    r"(?<![\w+])(?P<amount>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<unit>MT\b|MTS\b|KG\b|KGS\b|TONS?\b|FCL\b|CONTAINERS?\b|"
    r"DRUMS?\b|BAGS?\b|PCS\b|PIECES?\b)",
    re.I,
)
_NUMERIC_QUANTITY = re.compile(r"\s*\d[\d,]*(?:\.\d+)?\s*")
_QUOTE_START = re.compile(
    r"^\s*(?:-{2,}\s*(?:replied|original)\s+message\s*-{2,}|On\b.*\bwrote:\s*|>)",
    re.I,
)


def _own_message_text(text: str) -> str:
    """Keep the sender's new text before a quoted earlier email, if marked."""
    own_lines = []
    for line in text.splitlines():
        if _QUOTE_START.match(line):
            break
        own_lines.append(line)
    return "\n".join(own_lines).strip()


def _explicit_currency_prices(text: str) -> set[float]:
    """Find offered currency amounts, excluding amounts explicitly rejected in context."""
    values = set()
    for match in _CURRENCY_AMOUNT.finditer(text):
        # A supplier may mention a customer's target price while explicitly refusing it.
        # Such a reference is not an offer and must not make an otherwise complete
        # extraction fail (for example: "USD750 is too low, we can't accept it").
        # Limit the check to the same sentence/contrast clause so a rejected price
        # does not suppress a different price offered after "but".
        start = max(text.rfind(mark, 0, match.start()) for mark in ("\n", ".", "!", "?")) + 1
        end_candidates = [text.find(mark, match.end()) for mark in ("\n", ".", "!", "?")]
        end_candidates = [pos for pos in end_candidates if pos >= 0]
        end = min(end_candidates) if end_candidates else len(text)
        clause_start = start
        for contrast in re.finditer(r"\b(?:but|however)\b", text[start:match.start()], re.I):
            clause_start = start + contrast.end()
        clause_end = end
        contrast_after = re.search(r"\b(?:but|however)\b", text[match.end():end], re.I)
        if contrast_after:
            clause_end = match.end() + contrast_after.start()
        context = text[clause_start:clause_end]
        if _REJECTED_PRICE_CONTEXT.search(context):
            continue
        raw = match.group(1) or match.group(2)
        try:
            values.add(float(raw.replace(",", "")))
        except ValueError:
            continue
    return values


def _source_quantity_text(value, source_text: str) -> str | None:
    """Return a numeric quantity only when the source pairs it with a unit."""
    try:
        expected = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    for match in _QUANTITY_WITH_UNIT.finditer(source_text):
        if float(match["amount"].replace(",", "")) == expected:
            return match.group(0).strip()
    return None


def _is_minchem(sender: str | None) -> bool:
    return not sender or "minchem" in sender.lower()


def _validated_offers(raw: str, own_text: str) -> tuple[list[ThreadOffer] | None, str | None]:
    """Validate a model reply and reject omitted explicit prices without saving partial data."""
    try:
        data = _parse_llm_json(raw)
        if isinstance(data, list):
            data = {"offers": data}
        entries = data.get("offers") if isinstance(data, dict) else None
        if isinstance(entries, list):
            invalid = []
            for index, entry in enumerate(entries, start=1):
                if isinstance(entry, dict):
                    quantity = entry.get("quantity")
                    if isinstance(quantity, (int, float)) and not isinstance(quantity, bool):
                        stated_quantity = _source_quantity_text(quantity, own_text)
                        if stated_quantity is None:
                            invalid.append(f"offer {index}: numeric quantity has no matching unit in source")
                            continue
                        entry["quantity"] = stated_quantity
                    elif isinstance(quantity, str) and _NUMERIC_QUANTITY.fullmatch(quantity):
                        stated_quantity = _source_quantity_text(quantity, own_text)
                        if stated_quantity is None:
                            invalid.append(f"offer {index}: numeric quantity has no matching unit in source")
                            continue
                        entry["quantity"] = stated_quantity
                try:
                    ThreadOffer.model_validate(entry)
                except Exception as exc:
                    details = getattr(exc, "errors", lambda: [])()
                    fields = sorted({
                        ".".join(str(part) for part in error.get("loc", ())) or "offer"
                        for error in details
                    })
                    invalid.append(f"offer {index}: {', '.join(fields) or type(exc).__name__}")
            if invalid:
                return None, f"{len(invalid)} offer entr{'y' if len(invalid) == 1 else 'ies'} failed validation ({'; '.join(invalid)})"
        result = ThreadOffers.model_validate(data)
        sent = len(data.get("offers") or []) if isinstance(data, dict) else 0
        if sent != len(result.offers):
            return None, f"{sent - len(result.offers)} offer entry or entries failed validation"
        extracted = {
            float(price) for offer in result.offers
            for price in (offer.unit_price, offer.total_price) if price is not None
        }
        missing = sorted(_explicit_currency_prices(own_text) - extracted)
        if missing:
            return None, f"currency-labelled price(s) {missing} were omitted"
        return result.offers, None
    except Exception as exc:
        return None, f"model response could not be parsed or validated ({type(exc).__name__})"


def extract_message_offers(
    sender: str, text: str, issues: list[str] | None = None,
    source_type: str = "email",
) -> list[ThreadOffer] | None:
    """Extract one email or quotation PDF, retrying an incomplete reply once."""
    if source_type == "quotation":
        if len(text) > MAX_QUOTATION_CHARS:
            message = f"Quotation PDF text exceeds {MAX_QUOTATION_CHARS} characters; manual review is needed"
            if issues is not None:
                issues.append(message)
            print(f"      {message}")
            return None
        own_text = text
        chain = quotation_chain
    else:
        own_text = _own_message_text(text)[:MAX_MESSAGE_CHARS]
        chain = message_chain
    try:
        raw = chain.invoke({"sender": sender, "text": own_text} if source_type == "email" else {"text": own_text})
        offers, issue = _validated_offers(raw, own_text)
        if issue is None:
            return offers
        print(f"      Incomplete extraction: {issue}; retrying once")
        time.sleep(CALL_PAUSE_SECONDS)
        raw = repair_chain.invoke({"sender": sender, "text": own_text, "issue": issue})
        offers, issue = _validated_offers(raw, own_text)
        if issue is None:
            return offers
        message = f"Incomplete extraction after retry: {issue}"
    except Exception as exc:
        error = str(exc).lower()
        if "rate_limit_exceeded" in error or "quota" in error or "429" in error:
            raise SystemExit("Groq rate limit or quota reached. No offers from this source were changed; retry later.") from exc
        message = f"LLM request failed ({type(exc).__name__}); see backend log"
        print(f"      Offer extraction failed: {exc}")
    print(f"      {message}")
    if issues is not None:
        issues.append(message)
    return None


def extract_thread_offers(
    redo: bool = False,
    ids: set[int] | None = None,
    dry: bool = False,
    tender_id: str | None = None,
) -> dict:
    counts = {"documents": 0, "offers_saved": 0, "failed": 0, "no_offers": 0, "errors": []}

    # Existing offer rows are protected unless the caller explicitly requests redo.
    docs = get_offer_documents(redo)
    if ids:
        docs = [d for d in docs if d["id"] in ids]
    if tender_id:
        normalized_tender_id = normalize_tender_id(tender_id)
        docs = [d for d in docs if d["tender_id"] == normalized_tender_id]

    for doc in docs:
        if not doc["tender_id"]:
            print(f"[{doc['id']}] SKIPPED (not linked to a tender yet) {_safe(doc['file_name'])[:60]}")
            continue
        counts["documents"] += 1

        found = []   # (message, offer, source message sequence)
        failed = False
        # A quotation PDF is one source with potentially several price tiers.
        if doc["document_type"] == "quotation":
            messages = [{"sender": None, "sent": None, "text": doc["full_text"]}]
        else:
            # This is source order from the printed thread, not a parsed time axis.
            messages = list(reversed(split_thread(doc["full_text"])))
        for message_seq, msg in enumerate(messages, start=1):
            if doc["document_type"] == "quotation":
                issues = []
                offers = extract_message_offers(
                    "standalone supplier quotation PDF", msg["text"], issues,
                    source_type="quotation",
                )
                time.sleep(CALL_PAUSE_SECONDS)
                if offers is None:
                    counts["errors"].append(f"PDF #{doc['id']}: {issues[0] if issues else 'offer extraction failed'}")
                    failed = True
                    break
                found.extend((msg, offer, message_seq) for offer in offers)
                continue
            if _is_minchem(msg["sender"]) or not re.search(r"\d", _own_message_text(msg["text"])):
                continue
            issues = []
            offers = extract_message_offers(msg["sender"], msg["text"], issues)
            time.sleep(CALL_PAUSE_SECONDS)
            if offers is None:
                counts["errors"].append(f"PDF #{doc['id']}: {issues[0] if issues else 'offer extraction failed'}")
                failed = True
                break
            found.extend((msg, o, message_seq) for o in offers)

        if failed:
            counts["failed"] += 1
            if not dry:
                set_thread_offer_extraction_status(doc["id"], "failed")
            print(f"[{doc['id']}] FAILED, nothing changed | {_safe(doc['file_name'])[:60]}")
            continue
        if not found:
            counts["no_offers"] += 1
            if not dry:
                set_thread_offer_extraction_status(doc["id"], "no_offers")
            print(f"[{doc['id']}] no supplier offers | {_safe(doc['file_name'])[:60]}")
            continue

        print(f"[{doc['id']}] {len(found)} offer(s) | {doc['tender_id']} | {_safe(doc['file_name'])[:50]}")
        document_offers = []
        for seq, (msg, offer, message_seq) in enumerate(found, start=1):
            offer.offered_at = msg["sent"]
            offer.sender = msg["sender"]
            print(f"      #{seq} {_safe(msg['sent'])[:26]} | {_safe(msg['sender'])[:28]}"
                  f" | {offer.unit_price} {offer.currency} {_safe(str(offer.price_terms))}"
                  f" | {offer.kind} | {_safe(str(offer.specifications))[:30]}")
            if dry:
                continue
            document_offers.append({
                "tender_id": doc["tender_id"],
                "offer_seq": seq,
                "message_seq": message_seq,
                "offered_at": msg["sent"],
                "vendor_name": offer.vendor_company or msg["sender"],
                "body_text": msg["text"],
                "extracted_json": offer.model_dump_json(),
            })
        if not dry:
            counts["offers_saved"] += replace_document_offers(doc["id"], document_offers)

    return counts


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Extract supplier offers from linked email-thread and quotation PDFs.")
    parser.add_argument("--redo", action="store_true", help="re-extract all thread documents")
    parser.add_argument("--ids", type=int, nargs="+", help="re-extract only these document ids")
    parser.add_argument("--tender", help="extract only documents linked to this tender")
    parser.add_argument("--dry", action="store_true", help="print the offers, write nothing")
    args = parser.parse_args()
    print(extract_thread_offers(
        redo=args.redo,
        ids=set(args.ids) if args.ids else None,
        dry=args.dry,
        tender_id=args.tender,
    ))
