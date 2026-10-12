"""Compact V2 prompt builders for schema-specific Bedrock extraction calls."""

from __future__ import annotations

import json

from app.document_render_service import RenderedPage
from app.v2_models import EmailThreadSegmentationResult, V2DocumentType


def page_routing_prompt(pages: list[RenderedPage]) -> str:
    """Build the bounded prompt for page-level document routing.

    The returned prompt names every supplied page so the caller can reject a
    response that drops or invents page numbers.  It contains no source text
    and therefore leaves visual interpretation to the VLM.
    """
    page_numbers = [page.page_number for page in pages]
    return f"""You are routing ordered PDF page images for MinChem procurement.
The attached images, in order, are pages: {page_numbers}.

For every attached page, identify its apparent document type only when visible:
- sales_contract
- email_thread
- purchase_order
- shipping_document

If it is a shipping-document page, optionally identify one component:
bill_of_lading, commercial_invoice, packing_list, certificate_of_origin,
analysis_certificate, insurance_certificate, payment_instruction,
shipping_instruction, or other.

Do not treat product names, grades, quantities, PO numbers, contract numbers,
invoice numbers, or supplier names as tender IDs. Do not guess unreadable text.
For every result give a short exact visible evidence string and its page number.

Return JSON only in this exact shape:
{{"pages":[{{"page_number":1,"apparent_document_type":null,
"shipping_component_type":null,"evidence":[{{"page_number":1,
"evidence_text":"visible wording","message_index":null}}],"review_reasons":[]}}]}}
"""


def document_classification_prompt(page_numbers: list[int]) -> str:
    """Build the document-level classification prompt after page routing.

    This prompt is intentionally separate from type-specific extraction.  The
    calling pipeline supplies only selected representative images for the page
    numbers given, never an unbounded PDF.
    """
    return f"""You are classifying one MinChem procurement PDF using the attached
representative pages. The source pages are {page_numbers}.

Choose exactly one top-level document_type:
sales_contract, email_thread, purchase_order, shipping_document.

Shipping bundles remain shipping_document even when they contain an invoice,
packing list, B/L, certificate, insurance certificate, or payment instruction.
A printed conversation remains email_thread even when it mentions an attached
contract. Never infer missing tender IDs; extract one only if visibly labelled
as Tender ID, Tender Ref, RFQ No, or an explicit bracketed enquiry code.

Extract visibly printed PO, contract, B/L, and invoice numbers into references
using reference_type po_number, contract_number, bill_of_lading_number, or
invoice_number. For a shipping bundle, components must be objects with
component_type, start_page, end_page, and evidence; never return a bare string
such as "Commercial Invoice" in components.
For each extracted reference, set confidence from 0 to 1 only for how clearly
the printed identifier is visible; use null when you cannot judge it. Confidence
does not replace evidence and does not decide grouping.

Return JSON only in this exact shape:
{{"document_type":"...","tender_ids":[],"references":[{{"reference_type":"po_number","raw_value":"...","confidence":0.95,"evidence":[{{"page_number":1,"evidence_text":"...","message_index":null}}]}}],"components":[{{"component_type":"commercial_invoice","start_page":1,"end_page":1,"evidence":[{{"page_number":1,"evidence_text":"...","message_index":null}}]}}],
"evidence":[{{"page_number":1,"evidence_text":"visible wording",
"message_index":null}}],"review_reasons":[]}}
"""


def document_detail_extraction_prompt(document_type: V2DocumentType, page_numbers: list[int]) -> str:
    """Build a strict type-specific extraction prompt for supplied page images."""
    base = f"""You are extracting facts from a MinChem {document_type.value}.
The attached images, in order, are PDF pages {page_numbers}.
Return JSON only. Preserve source wording in raw fields. Use null or [] when a
fact is not visibly stated. Never infer a tender ID from a product, grade,
quantity, PO number, contract number, invoice number, or party name. Every
non-null fact needs short exact visual evidence with its source page.
Only objects inside a field named references must include confidence from 0 to
1 for printed-text clarity, or null when it cannot be judged. Do not add
confidence to shipping components, lines, or messages. Confidence must never
be inferred from the filename.
Allowed review reasons: no_tender_id, multiple_tender_ids,
classification_ambiguity, malformed_extraction, conflicting_reference,
uncertain_vendor, ambiguous_price, duplicate_candidate,
offer_acceptance_mismatch.
"""
    if document_type == V2DocumentType.PURCHASE_ORDER:
        return base + """\nExtract header facts and every visible PO line. Return exactly:
{"po_number":null,"buyer_name":null,"supplier_name":null,"order_date_raw":null,
"currency":null,"delivery_terms_raw":null,"payment_terms_raw":null,
"lines":[{"line_index":1,"product_raw":null,"specification_raw":null,
"quantity_raw":null,"quantity_value":null,"quantity_unit":null,"unit_price":null,
"line_total":null,"evidence":[{"page_number":1,"evidence_text":"...","message_index":null}]}],
"references":[],"evidence":[{"page_number":1,"evidence_text":"...","message_index":null}],"review_reasons":[]}
"""
    if document_type == V2DocumentType.SALES_CONTRACT:
        return base + """\nExtract contract header facts and every separately priced visible
line. Do not decide which offer was accepted. Return exactly:
{"contract_number":null,"seller_name":null,"buyer_name":null,"contract_date_raw":null,
"payment_terms_raw":null,"shipment_terms_raw":null,
"lines":[{"line_index":1,"product_raw":null,"specification_raw":null,
"quantity_raw":null,"quantity_value":null,"quantity_unit":null,"unit_price":null,
"price_currency":null,"price_unit":null,"incoterm":null,"named_place":null,
"evidence":[{"page_number":1,"evidence_text":"...","message_index":null}]}],
"references":[],"evidence":[{"page_number":1,"evidence_text":"...","message_index":null}],"review_reasons":[]}
"""
    if document_type == V2DocumentType.SHIPPING_DOCUMENT:
        return base + """\nTreat the PDF as a bundle. Identify each component and its
inclusive page range; retain disagreements rather than resolving them.
component_type is bill_of_lading, commercial_invoice, packing_list,
certificate_of_origin, analysis_certificate, insurance_certificate,
payment_instruction, shipping_instruction, or other. Return exactly:
{"components":[{"component_type":"commercial_invoice","start_page":1,"end_page":1,
"bill_of_lading_number":null,"invoice_number":null,"vessel":null,"origin":null,
"destination":null,"shipment_date_raw":null,"references":[],
"evidence":[{"page_number":1,"evidence_text":"...","message_index":null}]}],"review_reasons":[]}
"""
    if document_type == V2DocumentType.EMAIL_THREAD:
        return base + """\nSegment every visibly distinct printed email message in source
chronology. Do not convert messages into offers yet. Extract a reference only
when it is visibly labelled as a contract, PO, B/L, or invoice number; do not
infer a tender ID from a product, grade, quantity, party name, or reference.
An explicit parenthesized enquiry code such as (EXPSE_65_FZCOSE) or
(E_280224_1) is a tender_id and may be extracted with its visible evidence.
author_party is supplier, minchem, customer, or unknown. Return exactly:
{"messages":[{"message_index":1,"start_page":1,"end_page":1,"sender_raw":null,
"recipients_raw":[],"subject_raw":null,"sent_at_raw":null,"author_party":"unknown",
"references":[],
"body_evidence":[{"page_number":1,"evidence_text":"...","message_index":1}],"review_reasons":[]}],"review_reasons":[]}
"""
    raise ValueError(f"Unsupported V2 document type: {document_type}")


def thread_negotiation_extraction_prompt(
    pages: list[RenderedPage], segmentation: EmailThreadSegmentationResult
) -> str:
    """Build a constrained extraction prompt for one segmented thread window."""
    message_register = [
        {
            "message_index": message.message_index,
            "start_page": message.start_page,
            "end_page": message.end_page,
            "author_party": message.author_party.value,
            "sender_raw": message.sender_raw,
            "subject_raw": message.subject_raw,
        }
        for message in segmentation.messages
    ]
    return f"""You are extracting the commercial negotiation history from a
MinChem printed email thread. The attached images, in order, are source pages
{[page.page_number for page in pages]}. The previously validated message
segmentation is below; do not add, remove, or renumber messages:
{json.dumps(message_register, ensure_ascii=False)}

Create chronological events only for visible message content. event_type is:
supplier_offer, minchem_counter_offer, supplier_acceptance,
minchem_confirmation, supplier_rejection, information_request, or
non_commercial. A supplier offer/acceptance/rejection must have actor_party
supplier; a MinChem counter/confirmation must have actor_party minchem.

Keep each price tier or product as a separate commercial_lines item. Preserve
product, quantity, price, currency, price unit, Incoterm, named place,
delivery, payment, validity, packing, and remarks exactly as stated. Never
calculate, convert, select a winner, compare prices across trade bases, or
infer unstated fields. unit_price requires raw_price_text; incoterm requires
raw_price_terms. An acceptance without clearly repeated terms is still an
acceptance event with no invented price lines.

Every event and commercial line needs short exact visual evidence. All
evidence for an event must carry that event's message_index. Return JSON only:
{{"events":[{{"message_index":1,"event_index":1,
"event_type":"supplier_offer","actor_party":"supplier","vendor_name_raw":null,
"offered_at_raw":null,"raw_event_text":"...","commercial_lines":[{{"line_index":1,
"product_raw":null,"grade_or_specification_raw":null,"quantity_raw":null,
"quantity_value":null,"quantity_unit":null,"unit_price":null,"price_currency":null,
"price_unit":null,"raw_price_text":null,"incoterm":null,"named_place":null,
"raw_price_terms":null,"delivery_terms_raw":null,"payment_terms_raw":null,
"validity_raw":null,"packing_raw":null,"remarks_raw":null,
"evidence":[{{"page_number":1,"evidence_text":"...","message_index":1}}]}}],
"evidence":[{{"page_number":1,"evidence_text":"...","message_index":1}}],
"review_reasons":[]}}],"review_reasons":[]}}
"""
