"""
Document analysis: one LLM call per stored PDF returns its type, the kinds of
documents it contains, and the reference numbers printed in it. References are
saved in `tender_references` (tender_id left empty; linking is the next step).

Analyse every document not analysed yet:
    uv run python -m app.document_service
Re-analyse everything (after changing the prompt):
    uv run python -m app.document_service --redo
Re-analyse only some documents:
    uv run python -m app.document_service --ids 24 25 26
"""

import argparse
import json
import os
import re
import time

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_groq import ChatGroq

from app.classify_service import _parse_llm_json
from app.models import DocumentAnalysis
from app.tender_store import get_documents_to_analyze, save_document_analysis

MAX_TOTAL_CHARS = 10000   # text sent to the LLM per document
MAX_PAGE_CHARS = 4000
MIN_PAGE_CHARS = 400
CALL_PAUSE_SECONDS = 8    # documents are bigger than emails: stay under Groq's token-per-minute limit

# Own model instance: documents need a larger output budget than emails, and a
# reasoning model can otherwise spend it all thinking and return an empty answer.
doc_llm = ChatGroq(
    model=os.getenv("DOC_MODEL", "openai/gpt-oss-20b"),
    temperature=0,
    max_tokens=4096,
    reasoning_effort="low",
    api_key=os.getenv("GROQ_API_KEY"),
)

analysis_prompt = ChatPromptTemplate.from_template(
    """You are analyzing one PDF document from the files of MinChem, a company that trades
commodities between suppliers and customers (Minchem Impex Singapore / India).
The text below was extracted page by page (each page shortened). Some pages come from OCR
and may contain typos.

Return:

1. document_type: exactly one of
- purchase_order: a customer's order to MinChem
- sales_contract: a contract between MinChem and a supplier
- email_thread: a printout of one or more email messages
- shipping_documents: a pack that can contain invoices, packing lists, bill of lading, certificates, insurance, payment instructions
- quotation: a standalone price offer
- tender_rfq: a standalone request for quotation / enquiry
- other

2. contains: list of the kinds of documents inside, using these words where they fit:
purchase_order, sales_contract, email_messages, commercial_invoice, packing_list, bill_of_lading,
certificate_of_origin, certificate_of_analysis, certificate_of_quality_weight, insurance_certificate,
payment_instruction_letter, shipping_instructions, quotation, other
List only documents that are physically included in this PDF, not documents that are merely
mentioned or required in its text (a sales contract that lists the shipping documents the
seller must provide contains only sales_contract; an email that mentions an attached contract
contains only email_messages).

3. references: identifying numbers printed in the document, each with a ref_type:
- po_number (customer purchase order number)
- contract_number
- bl_number (bill of lading number)
- invoice_number
- tender_id (a Tender ID, Tender Ref or RFQ No, or the enquiry code that an email subject carries in
  brackets, for example (DOMSE/723/PASSE) in "Enquiry - White Fused Alumina (DOMSE/723/PASSE)";
  quantities such as (220MT) and product names are not codes)
Rules:
- Copy each number exactly as printed. One entry per distinct value.
- Include numbers the document cites as well as its own.
- Do NOT include product or grade codes (for example "RKCB"), dates, container numbers,
  phone numbers, postal codes, bank account numbers or other certificate numbers.
- Do NOT include a contract or PO number that is only mentioned as an earlier or different
  deal (for example "same price as Contract No. X signed in January").
- A number inside an attached file name shown in the text is allowed
  (for example the contract number in "contract bauxite 2024HFWX-1004.pdf").
- The document file name below is context only: take no references from it.
- If there are none, use an empty list.

4. parties: company names acting as buyer, seller, shipper or consignee.
5. products: product names or grades as written.
6. amount_text: total value or price as written (for example "USD 45600 CIF Mundra"), or null.
7. document_date: the document's own date as written, or null
   (for an email thread: the date of its first message).

Never guess: use null or an empty list when something is not stated.

File name: {file_name}
Pages: {page_count}

Text:
{text}

Respond with ONLY valid JSON, no other text, in exactly this shape:
{{"document_type": "...", "contains": [], "references": [{{"ref_type": "...", "ref_value": "..."}}], "parties": [], "products": [], "amount_text": null, "document_date": null}}"""
)

analysis_chain = analysis_prompt | doc_llm | StrOutputParser()


def _build_excerpt(full_text: str) -> str:
    """Shorten every page evenly (keeping its start and end) so that all pages are
    represented, not just the first ones."""
    pages = re.split(r"^--- page \d+ ---\n", full_text, flags=re.M)[1:]
    if not pages:
        pages = [full_text]

    budget = min(MAX_PAGE_CHARS, max(MIN_PAGE_CHARS, MAX_TOTAL_CHARS // len(pages)))
    parts = []
    for number, text in enumerate(pages, start=1):
        text = text.strip()
        if len(text) > budget:
            head = int(budget * 0.7)
            text = text[:head] + "\n[...]\n" + text[-(budget - head):]
        parts.append(f"--- page {number} ---\n{text}")
    return "\n\n".join(parts)


def analyze_document_text(file_name: str, page_count: int | None, full_text: str | None) -> DocumentAnalysis | None:
    """One LLM call. Returns a validated DocumentAnalysis, or None on any failure
    (so the caller leaves the document unanalysed instead of storing a guess)."""
    raw = ""
    try:
        raw = analysis_chain.invoke({
            "file_name": file_name,
            "page_count": page_count or 0,
            "text": _build_excerpt(full_text or ""),
        })
        return DocumentAnalysis.model_validate(_parse_llm_json(raw))
    except Exception as e:
        print(f"Document analysis failed: {e} | raw output ({len(raw)} chars): {_safe(raw[:200])!r}")
        return None


def _normalize_ref(value: str) -> str:
    return " ".join(value.upper().split())


def _safe(text: str) -> str:
    return str(text).encode("ascii", "replace").decode()


def analyze_pending_documents(redo: bool = False, ids: set[int] | None = None) -> dict:
    """Analyse documents with no document_type yet (all documents if redo, only the
    given ids if ids). Failed documents stay unanalysed and are retried on the next run."""
    counts = {"analyzed": 0, "failed": 0}

    docs = get_documents_to_analyze(redo or bool(ids))
    if ids:
        docs = [d for d in docs if d["id"] in ids]

    for doc in docs:
        result = analyze_document_text(doc["file_name"], doc["page_count"], doc["full_text"])

        if result is None:
            counts["failed"] += 1
            print(f"[{doc['id']}] FAILED  {_safe(doc['file_name'])}")
        else:
            refs = sorted({(r.ref_type.value, _normalize_ref(r.ref_value)) for r in result.references})
            save_document_analysis(
                doc["id"],
                document_type=result.document_type.value,
                contains_json=json.dumps(result.contains),
                analysis_json=result.model_dump_json(),
                references=refs,
            )
            counts["analyzed"] += 1
            print(f"[{doc['id']}] {result.document_type.value} | {_safe(doc['file_name'])[:60]}")
            print(f"      contains: {_safe(', '.join(result.contains))}")
            print(f"      refs: {_safe('; '.join(f'{t}={v}' for t, v in refs))}")

        time.sleep(CALL_PAUSE_SECONDS)

    return counts


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Classify stored PDFs and extract their reference numbers.")
    parser.add_argument("--redo", action="store_true", help="re-analyse documents that were already analysed")
    parser.add_argument("--ids", type=int, nargs="+", help="re-analyse only these document ids")
    args = parser.parse_args()
    print(analyze_pending_documents(redo=args.redo, ids=set(args.ids) if args.ids else None))