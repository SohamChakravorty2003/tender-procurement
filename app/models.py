from pydantic import BaseModel, Field, field_validator
from enum import StrEnum


class EmailRequest(BaseModel):
    recipient: str = Field(..., description="The email address to send the request to")
    subject: str = Field(..., description="The subject of the email")
    body: str = Field(..., description="The body content of the email")

class EmailResponse(BaseModel):
    id: str
    thread_id: str
    sender: str
    recipient: str
    subject: str
    date: str
    body: str
    snippet: str
    category: str
    tender_id: str | None = None
    email_role: str | None = None
    analysis_deleted: bool = False

class EmailListResponse(BaseModel):
    emails: list[EmailResponse]

class TenderOfferExtractionRequest(BaseModel):
    tender_id: str = Field(..., min_length=1, description="Tender whose linked email and PDF offers should be extracted")


class TenderAttachmentProcessingRequest(BaseModel):
    tender_id: str = Field(..., min_length=1, description="Tender whose Gmail PDF attachments should be processed")


class TenderDeletionPreviewRequest(BaseModel):
    tender_id: str = Field(..., min_length=1)


class TenderDeletionRequest(BaseModel):
    tender_id: str = Field(..., min_length=1)
    confirm_tender_id: str = Field(..., min_length=1)


class DeletedThreadRestoreRequest(BaseModel):
    email_id: str = Field(..., min_length=1)
    thread_id: str = Field(..., min_length=1)

class EmailCategory(StrEnum):
    QUOTATION_REQUEST = "quotation_request"
    FOLLOW_UP = "follow_up"
    ORDER_CONFIRMATION = "order_confirmation"
    SHIPMENT_UPDATE = "shipment_update"
    DOCUMENT_COLLECTION = "document_collection"
    ISSUE_RESOLUTION = "issue_resolution"
    OTHER = "other"

class EmailRole(StrEnum):
    TENDER_ISSUE = "tender_issue"
    VENDOR_BID = "vendor_bid"
    OTHER = "other"

class EmailAnalysisResult(BaseModel):
    category: EmailCategory
    email_role: EmailRole
    tender_id: str | None = None

class BidStatus(StrEnum):
    PENDING = "pending"
    EXTRACTED = "extracted"
    FAILED = "failed"


class BidExtraction(BaseModel):
    is_bid: bool
    vendor_company: str | None = None

    total_price: float | None = None
    unit_price: float | None = None
    currency: str | None = None
    price_terms: str | None = None
    price_text: str | None = None

    products_offered: list[str] | None = None
    quantity: str | None = None
    specifications: str | None = None

    delivery_days: int | None = None
    delivery_text: str | None = None

    payment_terms: str | None = None
    warranty_support: str | None = None
    vendor_experience: str | None = None
    validity: str | None = None
    exceptions_or_deviations: str | None = None

    attachments_referenced: bool = False
    
    @field_validator("products_offered", mode="before")
    @classmethod
    def _wrap_single_product(cls, v):
        if isinstance(v, str):
            v = v.strip()
            return [v] if v else None
        return v
        
    @field_validator("specifications", mode="before")
    @classmethod
    def _join_specifications(cls, v):
        # The model sometimes returns one entry per grade as a list.
        if isinstance(v, list):
            parts = [str(x).strip() for x in v if str(x).strip()]
            return "; ".join(parts) or None
        return v

class DocumentType(StrEnum):
    PURCHASE_ORDER = "purchase_order"
    SALES_CONTRACT = "sales_contract"
    EMAIL_THREAD = "email_thread"
    SHIPPING_DOCUMENTS = "shipping_documents"
    QUOTATION = "quotation"
    TENDER_RFQ = "tender_rfq"
    OTHER = "other"


class ReferenceType(StrEnum):
    PO_NUMBER = "po_number"
    CONTRACT_NUMBER = "contract_number"
    BL_NUMBER = "bl_number"
    INVOICE_NUMBER = "invoice_number"
    TENDER_ID = "tender_id"


class DocumentReference(BaseModel):
    ref_type: ReferenceType
    ref_value: str


class DocumentAnalysis(BaseModel):
    document_type: DocumentType
    contains: list[str] = []
    references: list[DocumentReference] = []
    parties: list[str] = []
    products: list[str] = []
    amount_text: str | None = None
    document_date: str | None = None

    @field_validator("contains", "parties", "products", mode="before")
    @classmethod
    def _as_list(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            return [v] if v.strip() else []
        return v

    @field_validator("references", mode="before")
    @classmethod
    def _clean_references(cls, v):
        # Drop entries with an unknown ref_type or an empty value instead of
        # failing the whole document.
        if not isinstance(v, list):
            return []
        valid = {t.value for t in ReferenceType}
        return [
            {"ref_type": r["ref_type"], "ref_value": str(r["ref_value"]).strip()}
            for r in v
            if isinstance(r, dict)
            and r.get("ref_type") in valid
            and str(r.get("ref_value") or "").strip()
        ]

class ThreadOffer(BidExtraction):
    """One supplier offer found inside an email-thread document."""
    is_bid: bool = True
    offered_at: str | None = None      # date of the message, as written
    sender: str | None = None          # supplier contact or company that sent it
    kind: str = "vendor_offer"         # vendor_offer | vendor_acceptance

    @field_validator("kind", mode="before")
    @classmethod
    def _valid_kind(cls, v):
        return v if v in ("vendor_offer", "vendor_acceptance") else "vendor_offer"


class ThreadOffers(BaseModel):
    offers: list[ThreadOffer] = []

    @field_validator("offers", mode="before")
    @classmethod
    def _drop_bad_offers(cls, v):
        # One malformed offer must not discard the others.
        if not isinstance(v, list):
            return []
        good = []
        for item in v:
            try:
                good.append(ThreadOffer.model_validate(item).model_dump())
            except Exception:
                continue
        return good

class ReviewResolution(BaseModel):
    action: str = Field(..., description="'confirm' or 'reject'")
    tender_id: str | None = Field(None, description="Required for 'confirm': one of the review's two tenders")
