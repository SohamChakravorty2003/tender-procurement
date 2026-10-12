"""In-memory V2 document routing pipeline built on bounded page VLM calls.

Persistence and asynchronous job execution are intentionally deferred.  These
functions establish the validation and batching behaviour that the future
worker will call before saving any document facts.
"""

from __future__ import annotations

from collections.abc import Callable

from app.bedrock_vlm_service import BedrockVlmConfig, BedrockVlmError, invoke_vlm_json
from app.document_render_service import RenderedPage, batch_rendered_pages, render_pdf_pages
from app.v2_extraction_service import extract_document_details, extract_thread_negotiation_events
from app.v2_models import (
    DocumentClassificationResult,
    EmailThreadSegmentationResult,
    PageRoutingBatchResult,
    PageRoutingResult,
    RenderedPageMetadata,
    ReviewReason,
    V2DocumentType,
    V2DocumentAnalysisResult,
    V2ExtractionWindowResult,
)
from app.v2_prompt_service import document_classification_prompt, page_routing_prompt


def _validate_routing_pages(result: PageRoutingBatchResult, pages: list[RenderedPage]) -> list[PageRoutingResult]:
    """Reject a routing result that omits, reorders, or invents source pages."""
    expected = [page.page_number for page in pages]
    actual = [item.page_number for item in result.pages]
    if actual != expected:
        raise BedrockVlmError(f"routing response pages {actual} did not match requested pages {expected}")
    for route in result.pages:
        if any(evidence.page_number != route.page_number for evidence in route.evidence):
            raise BedrockVlmError(f"routing evidence must stay on page {route.page_number}")
    return result.pages


def route_document_pages(
    pages: list[RenderedPage],
    *,
    client=None,
    config: BedrockVlmConfig | None = None,
) -> list[PageRoutingResult]:
    """Route all rendered pages through bounded VLM batches in source order."""
    routing: list[PageRoutingResult] = []
    for batch in batch_rendered_pages(pages):
        response = invoke_vlm_json(
            prompt=page_routing_prompt(batch),
            pages=batch,
            result_model=PageRoutingBatchResult,
            client=client,
            config=config,
        )
        routing.extend(_validate_routing_pages(response.result, batch))
    return routing


def select_representative_pages(
    pages: list[RenderedPage], routing: list[PageRoutingResult], *, maximum: int = 8
) -> list[RenderedPage]:
    """Select diverse routing evidence for document-level classification.

    Always retains the first and last page and then adds the first page for
    each observed type/component combination. Raises when routing provenance
    does not align with the rendered pages.
    """
    if maximum < 2:
        raise ValueError("maximum must be at least two")
    if not pages:
        raise ValueError("at least one rendered page is required")
    if [page.page_number for page in pages] != [item.page_number for item in routing]:
        raise ValueError("routing results must align exactly with rendered pages")

    chosen_numbers = {pages[0].page_number, pages[-1].page_number}
    seen_kinds: set[tuple[str | None, str | None]] = set()
    for item in routing:
        key = (
            item.apparent_document_type.value if item.apparent_document_type else None,
            item.shipping_component_type.value if item.shipping_component_type else None,
        )
        if key not in seen_kinds and len(chosen_numbers) < maximum:
            chosen_numbers.add(item.page_number)
            seen_kinds.add(key)
    return [page for page in pages if page.page_number in chosen_numbers]


def classify_routed_document(
    pages: list[RenderedPage],
    routing: list[PageRoutingResult],
    *,
    client=None,
    config: BedrockVlmConfig | None = None,
) -> DocumentClassificationResult:
    """Classify representative pages, retrying once if cited evidence was omitted."""
    selected = select_representative_pages(pages, routing)
    def classify(selected_pages: list[RenderedPage]):
        return invoke_vlm_json(
            prompt=document_classification_prompt([page.page_number for page in selected_pages]),
            pages=selected_pages,
            result_model=DocumentClassificationResult,
            client=client,
            config=config,
        )

    response = classify(selected)
    allowed_pages = {page.page_number for page in selected}
    missing_pages = {item.page_number for item in response.result.evidence} - allowed_pages
    if missing_pages:
        # A model can occasionally cite a page it saw during routing but that
        # was not selected for classification. Retry once with that precise
        # page added, keeping the classification request bounded to eight pages.
        selected_by_number = {page.page_number: page for page in selected}
        selected_by_number.update(
            {page.page_number: page for page in pages if page.page_number in missing_pages}
        )
        retry_numbers = sorted(selected_by_number)
        if len(retry_numbers) > 8:
            protected = {pages[0].page_number, pages[-1].page_number, *missing_pages}
            if len(protected) > 8:
                raise BedrockVlmError("classification cited too many unsupplied pages")
            retry_numbers = sorted(protected) + [
                number for number in retry_numbers if number not in protected
            ][:max(0, 8 - len(protected))]
        retry_pages = [page for page in pages if page.page_number in set(retry_numbers)]
        response = classify(retry_pages)
        allowed_pages = {page.page_number for page in retry_pages}
    if any(item.page_number not in allowed_pages for item in response.result.evidence):
        raise BedrockVlmError("classification cited a page that was not supplied")
    return response.result


def _detail_page_windows(document_type: V2DocumentType, pages: list[RenderedPage]) -> list[list[RenderedPage]]:
    """Create bounded detail windows without dropping pages or crossing limits.

    Email-thread windows overlap by one page so a message that crosses a page
    break has a complete visual context in at least one window. Results remain
    separate; a later persistence/review stage will perform any deduplication.
    """
    if document_type != V2DocumentType.EMAIL_THREAD:
        return batch_rendered_pages(pages)

    batches = batch_rendered_pages(pages, max_pages=3, max_total_bytes=8_500_000)
    windows: list[list[RenderedPage]] = []
    prior_last_page: RenderedPage | None = None
    for batch in batches:
        window = [prior_last_page, *batch] if prior_last_page else batch
        windows.append(window)
        prior_last_page = batch[-1]
    return windows


def extract_routed_document_details(
    pages: list[RenderedPage],
    classification: DocumentClassificationResult,
    *,
    client=None,
    config: BedrockVlmConfig | None = None,
) -> list[V2ExtractionWindowResult]:
    """Extract each bounded window and retain every response with its page scope."""
    windows: list[V2ExtractionWindowResult] = []
    for window in _detail_page_windows(classification.document_type, pages):
        try:
            detail = extract_document_details(
                classification.document_type,
                window,
                client=client,
                config=config,
            )
        except BedrockVlmError as exc:
            if (
                "page ranges must start and end on supplied pages" in str(exc)
                or "detail evidence cited unsupplied page" in str(exc)
            ):
                # Preserve the strict provenance rule, but do not fail an
                # otherwise valid PDF because one bounded detail window cited
                # an out-of-window page. The completed document is marked for
                # malformed extraction below when no usable detail remains.
                continue
            raise
        if (
            classification.document_type == V2DocumentType.SHIPPING_DOCUMENT
            and not getattr(detail, "components", None)
            and ReviewReason.MALFORMED_EXTRACTION not in classification.review_reasons
        ):
            classification.review_reasons.append(ReviewReason.MALFORMED_EXTRACTION)
        events = None
        if isinstance(detail, EmailThreadSegmentationResult) and detail.messages:
            events = extract_thread_negotiation_events(
                window,
                detail,
                client=client,
                config=config,
            )
        windows.append(
            V2ExtractionWindowResult(
                page_numbers=[page.page_number for page in window],
                detail=detail,
                negotiation_events=events,
            )
        )
    if (
        classification.document_type == V2DocumentType.SHIPPING_DOCUMENT
        and not windows
        and ReviewReason.MALFORMED_EXTRACTION not in classification.review_reasons
    ):
        classification.review_reasons.append(ReviewReason.MALFORMED_EXTRACTION)
    return windows


def analyze_pdf_document(
    pdf_bytes: bytes,
    *,
    include_details: bool = True,
    client=None,
    config: BedrockVlmConfig | None = None,
    on_stage: Callable[[str, int | None, int | None], None] | None = None,
) -> V2DocumentAnalysisResult:
    """Render, route, classify, and optionally extract a PDF without persistence.

    The caller is responsible for dispatching this work outside an HTTP event
    loop. Detail output is deliberately window-scoped: values are not silently
    merged across page boundaries before a review-capable persistence phase.
    """
    if on_stage:
        on_stage("rendering", None, None)
    pages = render_pdf_pages(pdf_bytes)
    if on_stage:
        on_stage("routing", 0, len(pages))
    routing = route_document_pages(pages, client=client, config=config)
    if on_stage:
        on_stage("classifying", 0, len(pages))
    classification = classify_routed_document(pages, routing, client=client, config=config)
    if on_stage and include_details:
        on_stage("extracting", 0, len(pages))
    detail_windows = (
        extract_routed_document_details(pages, classification, client=client, config=config)
        if include_details else []
    )
    return V2DocumentAnalysisResult(
        page_count=len(pages),
        rendered_pages=[
            RenderedPageMetadata(
                page_number=page.page_number,
                image_sha256=page.image_sha256,
                width_px=page.width_px,
                height_px=page.height_px,
                mime_type=page.mime_type,
            )
            for page in pages
        ],
        routing=routing,
        classification=classification,
        detail_windows=detail_windows,
    )
