"""Deterministic, evidence-preserving contract-to-offer reconciliation.

This service deliberately returns candidates and review states only. It does
not modify legacy bid ``accepted`` flags and cannot declare a human-confirmed
accepted offer from a price match alone.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from decimal import Decimal

from app.v2_models import (
    AcceptedOfferMatchStatus,
    AcceptedOfferReconciliationResult,
    CommercialLine,
    ContractLineOfferMatch,
    FieldComparisonStatus,
    NegotiationEvent,
    NegotiationEventType,
    NegotiationExtractionResult,
    OfferContractCandidate,
    OfferContractFieldComparison,
    ReviewReason,
    SalesContractExtractionResult,
    SalesContractLine,
)


_COMMERCIAL_SUPPLIER_EVENTS = {
    NegotiationEventType.SUPPLIER_OFFER,
    NegotiationEventType.SUPPLIER_ACCEPTANCE,
}
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%d-%m-%Y",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%d %B %Y",
    "%d %b %Y",
    "%B %d, %Y",
    "%b %d, %Y",
)


def _normalise_text(value: str) -> str:
    """Return a conservative text key for deterministic exact comparisons."""
    normalised = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[^\w]+", " ", normalised).strip()


def _display_decimal(value: Decimal | None) -> str | None:
    """Render a Decimal without losing the caller's null/missing distinction."""
    return None if value is None else format(value, "f")


def _compare_text(field_name: str, contract_value: str | None, offer_value: str | None) -> OfferContractFieldComparison:
    """Compare two stated text values; absent facts are not treated as equal."""
    if not contract_value or not offer_value:
        return OfferContractFieldComparison(
            field_name=field_name,
            status=FieldComparisonStatus.MISSING,
            contract_value=contract_value,
            offer_value=offer_value,
            rationale="one or both source values are not stated",
        )
    status = FieldComparisonStatus.COMPATIBLE if _normalise_text(contract_value) == _normalise_text(offer_value) else FieldComparisonStatus.CONFLICTING
    return OfferContractFieldComparison(
        field_name=field_name,
        status=status,
        contract_value=contract_value,
        offer_value=offer_value,
        rationale="normalised source values are equal" if status == FieldComparisonStatus.COMPATIBLE else "normalised source values differ",
    )


def _compare_decimal(field_name: str, contract_value: Decimal | None, offer_value: Decimal | None) -> OfferContractFieldComparison:
    """Compare two explicitly parsed numeric values without rounding or tolerance."""
    if contract_value is None or offer_value is None:
        return OfferContractFieldComparison(
            field_name=field_name,
            status=FieldComparisonStatus.MISSING,
            contract_value=_display_decimal(contract_value),
            offer_value=_display_decimal(offer_value),
            rationale="one or both source values are not stated",
        )
    status = FieldComparisonStatus.COMPATIBLE if contract_value == offer_value else FieldComparisonStatus.CONFLICTING
    return OfferContractFieldComparison(
        field_name=field_name,
        status=status,
        contract_value=_display_decimal(contract_value),
        offer_value=_display_decimal(offer_value),
        rationale="exact parsed values are equal" if status == FieldComparisonStatus.COMPATIBLE else "exact parsed values differ",
    )


def _parse_source_date(value: str | None):
    """Parse only a short allow-list of unambiguous written dates; otherwise None."""
    if not value:
        return None
    cleaned = " ".join(value.replace(".", " ").split())
    for date_format in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, date_format).date()
        except ValueError:
            continue
    return None


def _compare_timeline(contract_date: str | None, offered_at: str | None) -> OfferContractFieldComparison:
    """Require an offer date no later than its associated contract when parseable."""
    parsed_contract = _parse_source_date(contract_date)
    parsed_offer = _parse_source_date(offered_at)
    if not contract_date or not offered_at:
        status = FieldComparisonStatus.MISSING
        rationale = "one or both source dates are not stated"
    elif parsed_contract is None or parsed_offer is None:
        status = FieldComparisonStatus.NOT_ASSESSED
        rationale = "source date format is not safely parsed"
    elif parsed_offer <= parsed_contract:
        status = FieldComparisonStatus.COMPATIBLE
        rationale = "offer date is on or before the contract date"
    else:
        status = FieldComparisonStatus.CONFLICTING
        rationale = "offer date is after the contract date"
    return OfferContractFieldComparison(
        field_name="timeline",
        status=status,
        contract_value=contract_date,
        offer_value=offered_at,
        rationale=rationale,
    )


def _candidate_comparisons(
    contract: SalesContractExtractionResult,
    contract_line: SalesContractLine,
    event: NegotiationEvent,
    offer_line: CommercialLine,
) -> list[OfferContractFieldComparison]:
    """Compare all required commercial attributes without cross-field inference."""
    return [
        _compare_text("vendor", contract.seller_name, event.vendor_name_raw),
        _compare_text("product", contract_line.product_raw, offer_line.product_raw),
        _compare_text("grade_or_specification", contract_line.specification_raw, offer_line.grade_or_specification_raw),
        _compare_decimal("quantity", contract_line.quantity_value, offer_line.quantity_value),
        _compare_text("quantity_unit", contract_line.quantity_unit, offer_line.quantity_unit),
        _compare_decimal("unit_price", contract_line.unit_price, offer_line.unit_price),
        _compare_text("price_currency", contract_line.price_currency, offer_line.price_currency),
        _compare_text("price_unit", contract_line.price_unit, offer_line.price_unit),
        _compare_text("incoterm", contract_line.incoterm, offer_line.incoterm),
        _compare_text("named_place", contract_line.named_place, offer_line.named_place),
        _compare_timeline(contract.contract_date_raw, event.offered_at_raw),
    ]


def _candidate_status(comparisons: list[OfferContractFieldComparison]) -> AcceptedOfferMatchStatus:
    """Classify a candidate without upgrading it to human-confirmed acceptance."""
    statuses = {comparison.status for comparison in comparisons}
    if FieldComparisonStatus.CONFLICTING in statuses:
        return AcceptedOfferMatchStatus.REJECTED
    if statuses == {FieldComparisonStatus.COMPATIBLE}:
        return AcceptedOfferMatchStatus.CANDIDATE
    return AcceptedOfferMatchStatus.NEEDS_REVIEW


def _supplier_offer_lines(events: NegotiationExtractionResult):
    """Yield commercial lines stated by suppliers, never MinChem target prices."""
    for event in events.events:
        if event.event_type not in _COMMERCIAL_SUPPLIER_EVENTS:
            continue
        for line in event.commercial_lines:
            yield event, line


def reconcile_contract_offer_lines(
    contract: SalesContractExtractionResult,
    events: NegotiationExtractionResult,
) -> AcceptedOfferReconciliationResult:
    """Return deterministic supplier-offer candidates for each extracted contract line.

    A unique fully compatible line becomes ``candidate`` only. Confirmation is
    intentionally left to a review workflow with both source documents.
    """
    matches: list[ContractLineOfferMatch] = []
    for contract_line in contract.lines:
        candidates: list[OfferContractCandidate] = []
        for event, offer_line in _supplier_offer_lines(events):
            comparisons = _candidate_comparisons(contract, contract_line, event, offer_line)
            candidates.append(
                OfferContractCandidate(
                    message_index=event.message_index,
                    event_index=event.event_index,
                    offer_line_index=offer_line.line_index,
                    status=_candidate_status(comparisons),
                    comparisons=comparisons,
                    contract_evidence=contract_line.evidence,
                    offer_evidence=[*event.evidence, *offer_line.evidence],
                )
            )

        full_candidates = [item for item in candidates if item.status == AcceptedOfferMatchStatus.CANDIDATE]
        viable_reviews = [item for item in candidates if item.status == AcceptedOfferMatchStatus.NEEDS_REVIEW]
        if len(full_candidates) == 1:
            line_status = AcceptedOfferMatchStatus.CANDIDATE
            reasons: list[ReviewReason] = []
        elif len(full_candidates) > 1:
            line_status = AcceptedOfferMatchStatus.AMBIGUOUS
            reasons = [ReviewReason.OFFER_ACCEPTANCE_MISMATCH]
        elif viable_reviews:
            line_status = AcceptedOfferMatchStatus.NEEDS_REVIEW
            reasons = [ReviewReason.OFFER_ACCEPTANCE_MISMATCH]
        else:
            line_status = AcceptedOfferMatchStatus.REJECTED
            reasons = [ReviewReason.OFFER_ACCEPTANCE_MISMATCH]
        matches.append(
            ContractLineOfferMatch(
                contract_line_index=contract_line.line_index,
                status=line_status,
                candidates=candidates,
                review_reasons=reasons,
            )
        )
    return AcceptedOfferReconciliationResult(matches=matches)
