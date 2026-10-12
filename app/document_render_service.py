"""Render PDF pages into bounded, ordered image inputs for a vision model.

No files are written by this module.  Persistence of page images and metadata
will be added with the PostgreSQL/document-asset phase; callers can already
use the page number and SHA-256 to retain provenance.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Iterable

import pymupdf


DEFAULT_DPI = 180
MAX_LONG_EDGE_PX = 2_400
MAX_IMAGE_BYTES = 3_500_000
DEFAULT_BATCH_PAGE_LIMIT = 4
DEFAULT_BATCH_BYTE_LIMIT = 12_000_000
_JPEG_QUALITIES = (85, 75, 65, 55)


class PdfRenderError(ValueError):
    """Raised when a PDF page cannot become a safe vision-model input."""


@dataclass(frozen=True)
class RenderedPage:
    """One JPEG page and immutable provenance needed by later extraction."""

    page_number: int
    image_bytes: bytes
    image_sha256: str
    width_px: int
    height_px: int
    mime_type: str = "image/jpeg"


def _scale_for_page(page: pymupdf.Page, dpi: int, max_long_edge_px: int) -> float:
    """Return a render scale that respects the requested DPI and image dimension cap."""
    if dpi <= 0:
        raise PdfRenderError("dpi must be positive")
    if max_long_edge_px <= 0:
        raise PdfRenderError("max_long_edge_px must be positive")
    requested_scale = dpi / 72
    longest_requested_edge = max(page.rect.width, page.rect.height) * requested_scale
    return min(requested_scale, max_long_edge_px / longest_requested_edge)


def _as_bounded_jpeg(page: pymupdf.Page, *, dpi: int, max_long_edge_px: int, max_image_bytes: int) -> RenderedPage:
    """Render one page as JPEG or raise when it cannot fit the request budget."""
    scale = _scale_for_page(page, dpi, max_long_edge_px)
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
    for quality in _JPEG_QUALITIES:
        image_bytes = pixmap.tobytes("jpeg", jpg_quality=quality)
        if len(image_bytes) <= max_image_bytes:
            return RenderedPage(
                page_number=page.number + 1,
                image_bytes=image_bytes,
                image_sha256=sha256(image_bytes).hexdigest(),
                width_px=pixmap.width,
                height_px=pixmap.height,
            )
    raise PdfRenderError(
        f"page {page.number + 1} exceeds the {max_image_bytes} byte vision-image limit "
        "after JPEG compression"
    )


def _validate_page_numbers(page_numbers: Iterable[int] | None, page_count: int) -> list[int]:
    """Return sorted unique one-based page numbers or raise on invalid selection."""
    if page_numbers is None:
        return list(range(1, page_count + 1))
    selected = sorted(set(page_numbers))
    if not selected:
        raise PdfRenderError("at least one page must be selected")
    if selected[0] < 1 or selected[-1] > page_count:
        raise PdfRenderError(f"page selection must be between 1 and {page_count}")
    return selected


def render_pdf_pages(
    pdf_bytes: bytes,
    *,
    page_numbers: Iterable[int] | None = None,
    dpi: int = DEFAULT_DPI,
    max_long_edge_px: int = MAX_LONG_EDGE_PX,
    max_image_bytes: int = MAX_IMAGE_BYTES,
) -> list[RenderedPage]:
    """Render selected PDF pages in ascending order without writing files.

    Raises ``PdfRenderError`` for missing data, encrypted PDFs, invalid page
    selections, malformed PDFs, or pages that cannot fit the image budget.
    """
    if not pdf_bytes:
        raise PdfRenderError("PDF data is empty")
    try:
        with pymupdf.open(stream=pdf_bytes, filetype="pdf") as document:
            if document.needs_pass:
                raise PdfRenderError("PDF is password protected")
            selected = _validate_page_numbers(page_numbers, len(document))
            return [
                _as_bounded_jpeg(
                    document[page_number - 1],
                    dpi=dpi,
                    max_long_edge_px=max_long_edge_px,
                    max_image_bytes=max_image_bytes,
                )
                for page_number in selected
            ]
    except PdfRenderError:
        raise
    except Exception as exc:
        raise PdfRenderError(f"could not render PDF pages: {exc}") from exc


def batch_rendered_pages(
    pages: Iterable[RenderedPage],
    *,
    max_pages: int = DEFAULT_BATCH_PAGE_LIMIT,
    max_total_bytes: int = DEFAULT_BATCH_BYTE_LIMIT,
) -> list[list[RenderedPage]]:
    """Group ordered pages into bounded batches while preserving page order.

    Raises ``PdfRenderError`` if a page cannot fit a batch on its own.  The
    caller chooses a conservative model-specific batch limit; the default is
    deliberately much smaller than a whole-document request.
    """
    if max_pages <= 0 or max_total_bytes <= 0:
        raise PdfRenderError("batch limits must be positive")

    batches: list[list[RenderedPage]] = []
    current: list[RenderedPage] = []
    current_bytes = 0
    previous_page = 0
    for page in pages:
        if page.page_number <= previous_page:
            raise PdfRenderError("pages must be in ascending unique order")
        previous_page = page.page_number
        page_bytes = len(page.image_bytes)
        if page_bytes > max_total_bytes:
            raise PdfRenderError(f"page {page.page_number} exceeds the batch byte limit")
        exceeds_count = len(current) >= max_pages
        exceeds_bytes = current and current_bytes + page_bytes > max_total_bytes
        if exceeds_count or exceeds_bytes:
            batches.append(current)
            current = []
            current_bytes = 0
        current.append(page)
        current_bytes += page_bytes
    if current:
        batches.append(current)
    return batches
