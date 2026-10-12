"""Strict type-specific V2 extraction from explicitly supplied page images.

This service is intentionally transient: it validates visual evidence and
returns facts for a future V2 persistence/job phase. It never touches the
current SQLite document, bid, or tender tables.
"""

from __future__ import annotations

from typing import TypeAlias

from pydantic import BaseModel

from app.bedrock_vlm_service import BedrockVlmConfig, BedrockVlmError, invoke_vlm_json
from app.document_render_service import RenderedPage
from app.v2_models import (
    DocumentComponent,
    EmailThreadSegmentationResult,
    NegotiationEvent,
    NegotiationEventType,
    NegotiationExtractionResult,
    PurchaseOrderExtractionResult,
    SalesContractExtractionResult,
    ShippingComponent,
    ShippingExtractionResult,
    SourceEvidence,
    ThreadMessage,
    V2DocumentType,
)
from app.v2_prompt_service import (
    document_detail_extraction_prompt,
    thread_negotiation_extraction_prompt,
)


V2DetailResult: TypeAlias = (
    PurchaseOrderExtractionResult
    | SalesContractExtractionResult
    | ShippingExtractionResult
    | EmailThreadSegmentationResult
)

_RESULT_MODELS: dict[V2DocumentType, type[BaseModel]] = {
    V2DocumentType.PURCHASE_ORDER: PurchaseOrderExtractionResult,
    V2DocumentType.SALES_CONTRACT: SalesContractExtractionResult,
    V2DocumentType.SHIPPING_DOCUMENT: ShippingExtractionResult,
    V2DocumentType.EMAIL_THREAD: EmailThreadSegmentationResult,
}


def _walk_model_values(value: object):
    """Yield nested Pydantic models and container contents without coercion."""
    if isinstance(value, BaseModel):
        yield value
        for field_name in value.__class__.model_fields:
            yield from _walk_model_values(getattr(value, field_name))
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _walk_model_values(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk_model_values(item)


def _validate_detail_provenance(result: BaseModel, pages: list[RenderedPage]) -> None:
    """Reject detail facts or ranges whose provenance was not supplied to the VLM."""
    allowed_pages = {page.page_number for page in pages}
    for value in _walk_model_values(result):
        if isinstance(value, SourceEvidence) and value.page_number not in allowed_pages:
            raise BedrockVlmError(f"detail evidence cited unsupplied page {value.page_number}")
        if isinstance(value, (DocumentComponent, ShippingComponent, ThreadMessage)):
            if value.start_page not in allowed_pages or value.end_page not in allowed_pages:
                raise BedrockVlmError("detail page ranges must start and end on supplied pages")


def extract_document_details(
    document_type: V2DocumentType,
    pages: list[RenderedPage],
    *,
    client=None,
    config: BedrockVlmConfig | None = None,
) -> V2DetailResult:
    """Extract strict facts from an explicitly bounded ordered page window.

    A future aggregation phase must reconcile multi-window results; this
    function never drops pages or invents cross-window facts.
    """
    if not pages:
        raise ValueError("detail extraction requires at least one rendered page")
    result_model = _RESULT_MODELS[document_type]
    response = invoke_vlm_json(
        prompt=document_detail_extraction_prompt(document_type, [page.page_number for page in pages]),
        pages=pages,
        result_model=result_model,
        client=client,
        config=config,
    )
    _validate_detail_provenance(response.result, pages)
    return response.result  # type: ignore[return-value]


def _validate_thread_event_provenance(
    result: NegotiationExtractionResult,
    segmentation: EmailThreadSegmentationResult,
    pages: list[RenderedPage],
) -> None:
    """Reject events that conflict with segmentation, actors, or page evidence."""
    _validate_detail_provenance(result, pages)
    messages = {message.message_index: message for message in segmentation.messages}
    commercial_event_types = {
        NegotiationEventType.SUPPLIER_OFFER,
        NegotiationEventType.MINCHEM_COUNTER_OFFER,
        NegotiationEventType.SUPPLIER_ACCEPTANCE,
        NegotiationEventType.MINCHEM_CONFIRMATION,
        NegotiationEventType.SUPPLIER_REJECTION,
    }
    for event in result.events:
        message = messages.get(event.message_index)
        if message is None:
            raise BedrockVlmError(f"event referenced unknown message {event.message_index}")
        if event.event_type in commercial_event_types and message.author_party != event.actor_party:
            raise BedrockVlmError(
                f"event {event.message_index}/{event.event_index} conflicts with segmented message actor"
            )
        for value in _walk_model_values(event):
            if isinstance(value, SourceEvidence) and value.message_index != event.message_index:
                raise BedrockVlmError(
                    f"event {event.message_index}/{event.event_index} evidence must cite its own message"
                )


def extract_thread_negotiation_events(
    pages: list[RenderedPage],
    segmentation: EmailThreadSegmentationResult,
    *,
    client=None,
    config: BedrockVlmConfig | None = None,
) -> NegotiationExtractionResult:
    """Extract evidence-backed commercial events from a segmented thread window."""
    if not pages:
        raise ValueError("thread event extraction requires at least one rendered page")
    if not segmentation.messages:
        raise ValueError("thread event extraction requires at least one segmented message")
    page_numbers = {page.page_number for page in pages}
    if any(
        message.start_page not in page_numbers or message.end_page not in page_numbers
        for message in segmentation.messages
    ):
        raise ValueError("segmented message ranges must be within the supplied page window")
    response = invoke_vlm_json(
        prompt=thread_negotiation_extraction_prompt(pages, segmentation),
        pages=pages,
        result_model=NegotiationExtractionResult,
        client=client,
        config=config,
    )
    _validate_thread_event_provenance(response.result, segmentation, pages)
    return response.result
