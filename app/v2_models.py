"""Strict V2 extraction contracts for Bedrock document intelligence.

These models deliberately coexist with ``app.models`` while SQLite remains the
active store.  They accept only evidence-backed values: omitted facts stay
``None`` and invalid model output raises Pydantic validation errors.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictV2Model(BaseModel):
    """Reject unexpected model fields so malformed VLM output cannot persist."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class V2DocumentType(StrEnum):
    SALES_CONTRACT = "sales_contract"
    EMAIL_THREAD = "email_thread"
    PURCHASE_ORDER = "purchase_order"
    SHIPPING_DOCUMENT = "shipping_document"


class ProcessingState(StrEnum):
    UPLOADED = "uploaded"
    QUEUED = "queued"
    RENDERING = "rendering"
    ANALYZING = "analyzing"
    COMPLETED = "completed"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"


class ReferenceTypeV2(StrEnum):
    TENDER_ID = "tender_id"
    PO_NUMBER = "po_number"
    CONTRACT_NUMBER = "contract_number"
    BILL_OF_LADING_NUMBER = "bill_of_lading_number"
    INVOICE_NUMBER = "invoice_number"


class ShippingComponentType(StrEnum):
    BILL_OF_LADING = "bill_of_lading"
    COMMERCIAL_INVOICE = "commercial_invoice"
    PACKING_LIST = "packing_list"
    CERTIFICATE_OF_ORIGIN = "certificate_of_origin"
    ANALYSIS_CERTIFICATE = "analysis_certificate"
    INSURANCE_CERTIFICATE = "insurance_certificate"
    PAYMENT_INSTRUCTION = "payment_instruction"
    SHIPPING_INSTRUCTION = "shipping_instruction"
    OTHER = "other"


_SHIPPING_COMPONENT_ALIASES = {
    "certificate_of_analysis": "analysis_certificate",
    "certificate_of_quality": "analysis_certificate",
    "coa": "analysis_certificate",
    "bill_of_lading": "bill_of_lading",
    "commercial_invoice": "commercial_invoice",
    "packing_list": "packing_list",
    "certificate_of_insurance": "insurance_certificate",
    "insurance": "insurance_certificate",
    "certificate_of_quantity": "other",
    "certificate_of_weight": "other",
    "weight_certificate": "other",
    "certificate_of_quality_and_weight": "other",
    "test_certificate": "other",
    "test_and_guarantee_certificate": "other",
    "shipping_advice": "shipping_instruction",
    "contract": "other",
}

_SHIPPING_COMPONENT_VALUES = {item.value for item in ShippingComponentType}
_NON_TENDER_UNIT_REFERENCES = {
    "USD/MT", "USD/TON", "USD/TONNE", "USD/KG", "USD/CBM", "G/CM3", "G/CC",
}
_TENDER_ID_FORMAT = re.compile(r"^[A-Z][A-Z0-9]*(?:[\/_-][A-Z0-9]+)*$", re.IGNORECASE)
_PRODUCT_GRADE_REFERENCE = re.compile(r"^(?:RKCB|DBM|FM|SMP|MGO|AL2O3)\d.*$", re.IGNORECASE)


def _normalise_shipping_component(value):
    """Accept common source-label variants while retaining the strict enum."""
    if not isinstance(value, str):
        return value
    key = value.strip().lower().replace(" ", "_").replace("-", "_")
    return _SHIPPING_COMPONENT_ALIASES.get(key, key)


def _structured_references(value):
    """Keep only evidence-backed, groupable printed references.

    This remains intentionally conservative: a short number, product grade, or
    unit must not become an automatic tender-grouping edge.
    """
    if not isinstance(value, list):
        return []
    allowed = {item.value for item in ReferenceTypeV2}
    references = []
    for item in value:
        if not (
            isinstance(item, dict)
            and item.get("reference_type") in allowed
            and isinstance(item.get("raw_value"), str)
            and isinstance(item.get("evidence"), list)
            and item["evidence"]
        ):
            continue
        raw_value = item["raw_value"].strip()
        normalized = re.sub(r"[^A-Z0-9]", "", raw_value.upper())
        if len(normalized) < 5 or not any(char.isdigit() for char in normalized):
            continue
        if item["reference_type"] == ReferenceTypeV2.TENDER_ID.value:
            compact = re.sub(r"\s+", "", raw_value.upper())
            if (
                compact in _NON_TENDER_UNIT_REFERENCES
                or _PRODUCT_GRADE_REFERENCE.fullmatch(compact)
                or not _TENDER_ID_FORMAT.fullmatch(raw_value)
            ):
                continue
        references.append({**item, "raw_value": raw_value})
    return references


class ReviewReason(StrEnum):
    NO_TENDER_ID = "no_tender_id"
    MULTIPLE_TENDER_IDS = "multiple_tender_ids"
    CLASSIFICATION_AMBIGUITY = "classification_ambiguity"
    MALFORMED_EXTRACTION = "malformed_extraction"
    CONFLICTING_REFERENCE = "conflicting_reference"
    UNCERTAIN_VENDOR = "uncertain_vendor"
    AMBIGUOUS_PRICE = "ambiguous_price"
    DUPLICATE_CANDIDATE = "duplicate_candidate"
    OFFER_ACCEPTANCE_MISMATCH = "offer_acceptance_mismatch"


class ActorParty(StrEnum):
    SUPPLIER = "supplier"
    MINCHEM = "minchem"
    CUSTOMER = "customer"
    UNKNOWN = "unknown"


class NegotiationEventType(StrEnum):
    SUPPLIER_OFFER = "supplier_offer"
    MINCHEM_COUNTER_OFFER = "minchem_counter_offer"
    SUPPLIER_ACCEPTANCE = "supplier_acceptance"
    MINCHEM_CONFIRMATION = "minchem_confirmation"
    SUPPLIER_REJECTION = "supplier_rejection"
    INFORMATION_REQUEST = "information_request"
    NON_COMMERCIAL = "non_commercial"


class AcceptedOfferMatchStatus(StrEnum):
    """Outcome of deterministic contract-to-offer reconciliation."""

    CANDIDATE = "candidate"
    AMBIGUOUS = "ambiguous"
    REJECTED = "rejected"
    NEEDS_REVIEW = "needs_review"


class FieldComparisonStatus(StrEnum):
    """Compatibility state of one contract/offer attribute."""

    COMPATIBLE = "compatible"
    CONFLICTING = "conflicting"
    MISSING = "missing"
    NOT_ASSESSED = "not_assessed"


class SourceEvidence(StrictV2Model):
    """Verbatim visual evidence for a field; page number is one-based."""

    page_number: int = Field(ge=1)
    evidence_text: str = Field(min_length=1)
    message_index: int | None = Field(default=None, ge=1)


class ExtractedReference(StrictV2Model):
    """One printed identifier and the page evidence that supports it."""

    reference_type: ReferenceTypeV2
    raw_value: str = Field(min_length=1)
    confidence: float | None = Field(default=None, ge=0, le=1)
    evidence: list[SourceEvidence] = Field(min_length=1)


class DocumentComponent(StrictV2Model):
    """A contained shipping component, or an email message page range."""

    component_type: ShippingComponentType
    start_page: int = Field(ge=1)
    end_page: int = Field(ge=1)
    evidence: list[SourceEvidence] = Field(min_length=1)

    @field_validator("component_type", mode="before")
    @classmethod
    def _normalise_component_type(cls, value):
        return _normalise_shipping_component(value)

    @model_validator(mode="after")
    def _validate_page_range(self) -> "DocumentComponent":
        if self.end_page < self.start_page:
            raise ValueError("end_page must not precede start_page")
        return self


class DocumentClassificationResult(StrictV2Model):
    """Validated top-level routing result for exactly one V2 document type."""

    document_type: V2DocumentType
    tender_ids: list[ExtractedReference] = Field(default_factory=list)
    references: list[ExtractedReference] = Field(default_factory=list)
    components: list[DocumentComponent] = Field(default_factory=list)
    evidence: list[SourceEvidence] = Field(min_length=1)
    review_reasons: list[ReviewReason] = Field(default_factory=list)

    @field_validator("tender_ids", "references", mode="before")
    @classmethod
    def _keep_valid_references(cls, value):
        return _structured_references(value)

    @field_validator("components", mode="before")
    @classmethod
    def _discard_unstructured_components(cls, value):
        """Ignore shorthand component names; shipping extraction supplies evidence later."""
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, dict)]

    @model_validator(mode="after")
    def _validate_tender_references(self) -> "DocumentClassificationResult":
        if any(item.reference_type != ReferenceTypeV2.TENDER_ID for item in self.tender_ids):
            raise ValueError("tender_ids may contain only tender_id references")
        if self.document_type != V2DocumentType.SHIPPING_DOCUMENT:
            self.components = []
        return self


class PageRoutingResult(StrictV2Model):
    """Page-level routing signal used before document-level aggregation."""

    page_number: int = Field(ge=1)
    apparent_document_type: V2DocumentType | None = None
    shipping_component_type: ShippingComponentType | None = None
    evidence: list[SourceEvidence] = Field(default_factory=list)
    review_reasons: list[ReviewReason] = Field(default_factory=list)

    @field_validator("apparent_document_type", mode="before")
    @classmethod
    def _normalise_component_as_shipping_document(cls, value):
        """Treat a component-only page label as a shipping-document page."""
        if not isinstance(value, str):
            return value
        normalized = _normalise_shipping_component(value)
        if normalized in _SHIPPING_COMPONENT_VALUES - {ShippingComponentType.OTHER.value}:
            return V2DocumentType.SHIPPING_DOCUMENT.value
        return value


class PageRoutingBatchResult(StrictV2Model):
    """The complete page-routing response for one bounded image batch."""

    pages: list[PageRoutingResult] = Field(min_length=1)


class RenderedPageMetadata(StrictV2Model):
    """Non-binary rendering provenance returned by the V2 analysis endpoint."""

    page_number: int = Field(ge=1)
    image_sha256: str = Field(min_length=64, max_length=64)
    width_px: int = Field(ge=1)
    height_px: int = Field(ge=1)
    mime_type: str = Field(pattern=r"^image/jpeg$")


class ThreadMessage(StrictV2Model):
    """A message segmented from a printed email-thread document."""

    message_index: int = Field(ge=1)
    start_page: int = Field(ge=1)
    end_page: int = Field(ge=1)
    sender_raw: str | None = None
    recipients_raw: list[str] = Field(default_factory=list)
    subject_raw: str | None = None
    sent_at_raw: str | None = None
    author_party: ActorParty
    references: list[ExtractedReference] = Field(default_factory=list)
    body_evidence: list[SourceEvidence] = Field(min_length=1)
    review_reasons: list[ReviewReason] = Field(default_factory=list)

    @field_validator("references", mode="before")
    @classmethod
    def _keep_valid_references(cls, value):
        """Ignore malformed reference objects; labelled body text is retained separately."""
        return _structured_references(value)

    @model_validator(mode="after")
    def _validate_page_range(self) -> "ThreadMessage":
        if self.end_page < self.start_page:
            raise ValueError("end_page must not precede start_page")
        return self


class EmailThreadSegmentationResult(StrictV2Model):
    """All recoverable messages in one email-thread PDF, in source chronology."""

    messages: list[ThreadMessage]
    review_reasons: list[ReviewReason] = Field(default_factory=list)

    @field_validator("messages")
    @classmethod
    def _require_unique_message_indexes(cls, value: list[ThreadMessage]) -> list[ThreadMessage]:
        indexes = [message.message_index for message in value]
        if len(indexes) != len(set(indexes)):
            raise ValueError("message_index values must be unique")
        return value


class CommercialLine(StrictV2Model):
    """One stated commercial tier. Raw text is preserved beside parsed values."""

    line_index: int = Field(ge=1)
    product_raw: str | None = None
    grade_or_specification_raw: str | None = None
    quantity_raw: str | None = None
    quantity_value: Decimal | None = Field(default=None, ge=0)
    quantity_unit: str | None = None
    unit_price: Decimal | None = Field(default=None, ge=0)
    price_currency: str | None = None
    price_unit: str | None = None
    raw_price_text: str | None = None
    incoterm: str | None = None
    named_place: str | None = None
    raw_price_terms: str | None = None
    delivery_terms_raw: str | None = None
    payment_terms_raw: str | None = None
    validity_raw: str | None = None
    packing_raw: str | None = None
    remarks_raw: str | None = None
    evidence: list[SourceEvidence] = Field(min_length=1)

    @field_validator("unit_price", mode="before")
    @classmethod
    def _discard_non_scalar_unit_price(cls, value):
        """Keep raw price wording but never coerce a structured model fragment into a price."""
        return value if isinstance(value, (str, int, float, Decimal)) else None

    @model_validator(mode="after")
    def _validate_price_fields(self) -> "CommercialLine":
        if self.unit_price is not None and not self.raw_price_text:
            raise ValueError("unit_price requires raw_price_text evidence")
        if self.incoterm is not None and not self.raw_price_terms:
            raise ValueError("incoterm requires raw_price_terms evidence")
        return self


class NegotiationEvent(StrictV2Model):
    """One message-level commercial event, which may have many offer lines."""

    message_index: int = Field(ge=1)
    event_index: int = Field(ge=1)
    event_type: NegotiationEventType
    actor_party: ActorParty
    vendor_name_raw: str | None = None
    offered_at_raw: str | None = None
    raw_event_text: str = Field(min_length=1)
    commercial_lines: list[CommercialLine] = Field(default_factory=list)
    evidence: list[SourceEvidence] = Field(min_length=1)
    review_reasons: list[ReviewReason] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_actor_and_event(self) -> "NegotiationEvent":
        supplier_events = {
            NegotiationEventType.SUPPLIER_OFFER,
            NegotiationEventType.SUPPLIER_ACCEPTANCE,
            NegotiationEventType.SUPPLIER_REJECTION,
        }
        minchem_events = {
            NegotiationEventType.MINCHEM_COUNTER_OFFER,
            NegotiationEventType.MINCHEM_CONFIRMATION,
        }
        if self.event_type in supplier_events and self.actor_party != ActorParty.SUPPLIER:
            raise ValueError("supplier events require a supplier actor")
        if self.event_type in minchem_events and self.actor_party != ActorParty.MINCHEM:
            raise ValueError("MinChem events require a MinChem actor")
        return self


class NegotiationExtractionResult(StrictV2Model):
    """Extracted event history for a segmented email-thread document."""

    events: list[NegotiationEvent]
    review_reasons: list[ReviewReason] = Field(default_factory=list)

    @field_validator("events")
    @classmethod
    def _require_unique_event_positions(cls, value: list[NegotiationEvent]) -> list[NegotiationEvent]:
        keys = [(event.message_index, event.event_index) for event in value]
        if len(keys) != len(set(keys)):
            raise ValueError("each message/event position must be unique")
        return value


class PurchaseOrderLine(StrictV2Model):
    """One evidence-backed PO table row."""

    line_index: int = Field(ge=1)
    product_raw: str | None = None
    specification_raw: str | None = None
    quantity_raw: str | None = None
    quantity_value: Decimal | None = Field(default=None, ge=0)
    quantity_unit: str | None = None
    unit_price: Decimal | None = Field(default=None, ge=0)
    line_total: Decimal | None = Field(default=None, ge=0)
    evidence: list[SourceEvidence] = Field(min_length=1)


class PurchaseOrderExtractionResult(StrictV2Model):
    """Header and line items from a purchase order; missing fields remain null."""

    po_number: str | None = None
    buyer_name: str | None = None
    supplier_name: str | None = None
    order_date_raw: str | None = None
    currency: str | None = None
    delivery_terms_raw: str | None = None
    payment_terms_raw: str | None = None
    lines: list[PurchaseOrderLine]
    references: list[ExtractedReference] = Field(default_factory=list)
    evidence: list[SourceEvidence] = Field(min_length=1)
    review_reasons: list[ReviewReason] = Field(default_factory=list)


class SalesContractLine(StrictV2Model):
    """One contract line used later for deterministic offer reconciliation."""

    line_index: int = Field(ge=1)
    product_raw: str | None = None
    specification_raw: str | None = None
    quantity_raw: str | None = None
    quantity_value: Decimal | None = Field(default=None, ge=0)
    quantity_unit: str | None = None
    unit_price: Decimal | None = Field(default=None, ge=0)
    price_currency: str | None = None
    price_unit: str | None = None
    incoterm: str | None = None
    named_place: str | None = None
    evidence: list[SourceEvidence] = Field(min_length=1)


class SalesContractExtractionResult(StrictV2Model):
    """Header and line items from a supplier sales contract."""

    contract_number: str | None = None
    seller_name: str | None = None
    buyer_name: str | None = None
    contract_date_raw: str | None = None
    payment_terms_raw: str | None = None
    shipment_terms_raw: str | None = None
    lines: list[SalesContractLine]
    references: list[ExtractedReference] = Field(default_factory=list)
    evidence: list[SourceEvidence] = Field(min_length=1)
    review_reasons: list[ReviewReason] = Field(default_factory=list)


class ShippingComponent(StrictV2Model):
    """One component in a shipping-document bundle."""

    component_type: ShippingComponentType
    start_page: int = Field(ge=1)
    end_page: int = Field(ge=1)
    bill_of_lading_number: str | None = None
    invoice_number: str | None = None
    vessel: str | None = None
    origin: str | None = None
    destination: str | None = None
    shipment_date_raw: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    references: list[ExtractedReference] = Field(default_factory=list)
    evidence: list[SourceEvidence] = Field(min_length=1)

    @field_validator("component_type", mode="before")
    @classmethod
    def _normalise_component_type(cls, value):
        return _normalise_shipping_component(value)

    @field_validator("references", mode="before")
    @classmethod
    def _keep_structured_references(cls, value):
        return _structured_references(value)

    @model_validator(mode="after")
    def _validate_page_range(self) -> "ShippingComponent":
        if self.end_page < self.start_page:
            raise ValueError("end_page must not precede start_page")
        return self


class ShippingExtractionResult(StrictV2Model):
    """Shipping bundle components; an empty result remains reviewable, not failed."""

    components: list[ShippingComponent] = Field(default_factory=list)
    review_reasons: list[ReviewReason] = Field(default_factory=list)


class OfferContractFieldComparison(StrictV2Model):
    """One deterministic comparison; values remain display/audit evidence."""

    field_name: str = Field(min_length=1)
    status: FieldComparisonStatus
    contract_value: str | None = None
    offer_value: str | None = None
    rationale: str = Field(min_length=1)


class OfferContractCandidate(StrictV2Model):
    """One supplier-line candidate considered for a sales-contract line."""

    message_index: int = Field(ge=1)
    event_index: int = Field(ge=1)
    offer_line_index: int = Field(ge=1)
    status: AcceptedOfferMatchStatus
    comparisons: list[OfferContractFieldComparison] = Field(min_length=1)
    contract_evidence: list[SourceEvidence] = Field(min_length=1)
    offer_evidence: list[SourceEvidence] = Field(min_length=1)


class ContractLineOfferMatch(StrictV2Model):
    """All possible supplier sources for one contract line, without acceptance."""

    contract_line_index: int = Field(ge=1)
    status: AcceptedOfferMatchStatus
    candidates: list[OfferContractCandidate] = Field(default_factory=list)
    review_reasons: list[ReviewReason] = Field(default_factory=list)


class AcceptedOfferReconciliationResult(StrictV2Model):
    """Candidate/review output; a human workflow alone may later confirm it."""

    matches: list[ContractLineOfferMatch] = Field(default_factory=list)
    review_reasons: list[ReviewReason] = Field(default_factory=list)


class V2ExtractionWindowResult(StrictV2Model):
    """One bounded extraction response, retained without cross-window merging."""

    page_numbers: list[int] = Field(min_length=1)
    detail: (
        PurchaseOrderExtractionResult
        | SalesContractExtractionResult
        | ShippingExtractionResult
        | EmailThreadSegmentationResult
    )
    negotiation_events: NegotiationExtractionResult | None = None


class V2DocumentAnalysisResult(StrictV2Model):
    """Transient V2 analysis output before persistence and review workflows exist."""

    page_count: int = Field(ge=1)
    rendered_pages: list[RenderedPageMetadata] = Field(min_length=1)
    routing: list[PageRoutingResult] = Field(min_length=1)
    classification: DocumentClassificationResult
    detail_windows: list[V2ExtractionWindowResult] = Field(default_factory=list)
