"""
PDF ingestion: text extraction with a per-page quality check and OCR fallback.

Per page:
  1. Read the PDF text layer.
  2. Judge its quality (empty / garbled / ok) and whether the page is mostly
     a scanned image.
  3. If the text layer is bad, or the page is a scan, OCR the rendered page
     and use the OCR text when it is usable.
The result is stored in the `documents` table, de-duplicated by sha256.

Dry run on one file (no database write):
    uv run python -m app.pdf_service "path\\to\\file.pdf"
Save to the database:
    uv run python -m app.pdf_service "path\\to\\file.pdf" --save
"""

import argparse
import hashlib
import json
import re
from pathlib import Path

import pymupdf

from app.tender_store import get_document_by_sha, save_document

MIN_CHARS = 30            # fewer characters than this = page has no usable text
OCR_DPI = 200
SCANNED_COVERAGE = 0.5    # image covering >= 50% of the page = scanned page

_ocr_engine = None


def _get_ocr():
    """Load RapidOCR on first use (model load is slow; first run may download models)."""
    global _ocr_engine
    if _ocr_engine is None:
        from rapidocr import RapidOCR
        _ocr_engine = RapidOCR()
    return _ocr_engine


def _ocr_page(page) -> str:
    import numpy as np

    pix = page.get_pixmap(dpi=OCR_DPI, alpha=False)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)

    out = _get_ocr()(img)
    txts = getattr(out, "txts", None)
    if txts is None and isinstance(out, tuple):  # older RapidOCR: (result, elapsed)
        txts = [r[1] for r in out[0]] if out[0] else []
    return "\n".join(txts or []).strip()


def _image_coverage(page) -> float:
    area = page.rect.width * page.rect.height
    if area <= 0:
        return 0.0
    covered = 0.0
    for info in page.get_image_info():
        r = pymupdf.Rect(info["bbox"]) & page.rect
        if not r.is_empty:
            covered += r.width * r.height
    return min(1.0, covered / area)


def _assess(text: str) -> tuple[bool, str]:
    """Return (usable, reason). reason is 'ok', 'empty' or 'garbled'."""
    t = text.strip()
    if len(t) < MIN_CHARS:
        return False, "empty"

    total = len(t)
    bad = sum(
        1 for ch in t
        if ch == "�"
        or (ord(ch) < 32 and ch not in "\n\r\t")
        or 0xE000 <= ord(ch) <= 0xF8FF
    )
    if re.search(r"\(cid:\d+\)", t) or bad / total > 0.05:
        return False, "garbled"

    symbols = sum(1 for ch in t if not ch.isalnum() and not ch.isspace())
    if symbols / total > 0.35:
        return False, "garbled"

    words = re.findall(r"[A-Za-z]{3,}", t)
    if len(words) >= 15:
        with_vowel = sum(1 for w in words if re.search(r"[aeiouyAEIOUY]", w))
        if with_vowel / len(words) < 0.7:
            return False, "garbled"

    return True, "ok"


def extract_pdf(data: bytes) -> dict:
    """
    Extract text from PDF bytes. Returns:
      {"page_count", "pages": [{page, method, quality, chars, scanned}],
       "full_text", "status"}
    method is "text" (text layer) or "ocr"; quality is "ok", "empty" or "garbled"
    (the quality of the text finally used). status is "ok" when every page ended
    up usable, otherwise "partial".
    """
    pages_info = []
    chunks = []

    with pymupdf.open(stream=data, filetype="pdf") as doc:
        if doc.needs_pass:
            raise ValueError("PDF is password protected")

        for number, page in enumerate(doc, start=1):
            layer = page.get_text("text").strip()
            layer_ok, _ = _assess(layer)
            scanned = _image_coverage(page) >= SCANNED_COVERAGE

            text, method = layer, "text"
            if not layer_ok or scanned:
                ocr_text = _ocr_page(page)
                if len(ocr_text) >= MIN_CHARS and (not layer_ok or len(ocr_text) >= 0.8 * len(layer)):
                    text, method = ocr_text, "ocr"

            usable, reason = _assess(text)
            pages_info.append({
                "page": number,
                "method": method,
                "quality": "ok" if usable else reason,
                "chars": len(text),
                "scanned": scanned,
            })
            chunks.append(f"--- page {number} ---\n{text}")

    status = "ok" if all(p["quality"] == "ok" for p in pages_info) else "partial"
    return {
        "page_count": len(pages_info),
        "pages": pages_info,
        "full_text": "\n\n".join(chunks),
        "status": status,
    }


def ingest_pdf(
    data: bytes,
    file_name: str,
    *,
    source: str = "upload",
    email_id: str | None = None,
    gmail_attachment_id: str | None = None,
) -> dict:
    """
    Extract and store one PDF. A file already stored (same sha256) is not
    re-processed. Returns {"document_id", "is_new", "status", "page_count"}.
    """
    sha256 = hashlib.sha256(data).hexdigest()

    existing = get_document_by_sha(sha256)
    if existing:
        return {
            "document_id": existing["id"],
            "is_new": False,
            "status": existing["extraction_status"],
            "page_count": existing["page_count"],
        }

    result = extract_pdf(data)
    document_id, is_new = save_document(
        source=source,
        file_name=file_name,
        sha256=sha256,
        email_id=email_id,
        gmail_attachment_id=gmail_attachment_id,
        page_count=result["page_count"],
        full_text=result["full_text"],
        page_quality_json=json.dumps(result["pages"]),
        extraction_status=result["status"],
    )
    return {
        "document_id": document_id,
        "is_new": is_new,
        "status": result["status"],
        "page_count": result["page_count"],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Extract text from a PDF (dry run unless --save).")
    parser.add_argument("path")
    parser.add_argument("--save", action="store_true", help="store the result in procurement.db")
    args = parser.parse_args()

    pdf_path = Path(args.path)
    pdf_bytes = pdf_path.read_bytes()

    result = extract_pdf(pdf_bytes)
    print(f"{pdf_path.name}: {result['page_count']} pages, status={result['status']}")
    print("page  method  quality  chars  scanned")
    for p in result["pages"]:
        print(f"{p['page']:>4}  {p['method']:<6}  {p['quality']:<7}  {p['chars']:>5}  {p['scanned']}")

    for p in result["pages"]:
        if p["method"] == "ocr":
            start = result["full_text"].index(f"--- page {p['page']} ---")
            preview = result["full_text"][start:start + 160]
            print(f"\nOCR preview, page {p['page']}:")
            print(preview.encode("ascii", "replace").decode())

    if args.save:
        print("\nSaved:", ingest_pdf(pdf_bytes, pdf_path.name))
