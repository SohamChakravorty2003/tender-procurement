# Procurement Case Extraction, Relationship, and UI Design

## Goal

Extend the existing tender-based application with a procurement-case layer.
This must improve structure without making tender grouping more aggressive.

A tender remains a formal tender identifier when one exists. A procurement case
is the internal parent grouping for a commercial deal that can include multiple
contracts, purchase orders, shipments, invoices, and shared correspondence.

```text
Procurement case
├─ Contract
├─ Purchase order A → Shipment A → invoices / B-L / certificates
├─ Purchase order B → Shipment B → invoices / B-L / certificates
└─ Shared emails and documents
```

## Mahakoshal Target Structure

```text
Case: Mahakoshal / RKCB / 2024HFWX-1004
├─ Contract: 2024HFWX-1004
├─ Gudri PO: MRPLG...007
│  └─ Shipment: JAM2412610, 220 MT, Gangavaram
└─ Bhachau PO: MRPLB...003
   └─ Shipment: TMSE3279887, 110 MT, Mundra
```

The current `TND-0001` grouping can remain as a compatible legacy/display
association during migration, but it must not be the only representation of
the deal.

## Backend Design

### 1. Storage and migrations

All SQL and migrations remain in `app/tender_store.py`. Additive migrations
must preserve the existing `v2_documents`, `v2_references`, review cases,
grouping states, and tender associations.

Add the following tables:

- `procurement_cases`
  - Internal case ID, display name, status, creation source, timestamps.
  - Optional link to a formal tender; never invent a tender ID.

- `v2_document_segments`
  - One row for each logical component of an uploaded PDF.
  - Page range, component type, extracted data, and supporting evidence.
  - Supports a shipping bundle containing a B/L, invoice, packing list, and
    certificate without treating the entire PDF as one document type.

- `v2_business_entities`
  - Typed records for `contract`, `purchase_order`, `shipment`, `invoice`,
    and `email_thread`.
  - Retain raw and normalized identifiers together.

- `v2_entity_document_links`
  - Links a document or document segment to an entity.
  - Stores relationship type, page evidence, match method, confidence, and
    review status.

- `v2_entity_relationships`
  - Links entities with typed relationships, for example contract to case,
    PO to case, shipment fulfilling PO, or invoice referencing shipment.

- `v2_discrepancies`
  - Stores identity, commercial, quantity, and logistics conflicts separately
    from otherwise valid relationships.

### 2. Extraction

Extend `app/v2_models.py` and `app/v2_document_pipeline.py`.

- Expand supported logical component types to commercial invoice, proforma
  invoice, packing list, B/L, certificates, and email correspondence.
- Extract logical document segments and their page ranges.
- Keep typed references with raw value, normalized value, confidence, and
  source-page evidence.
- Extract line items only as supporting evidence: grade, size, quantity, unit,
  price, currency, ports, and dates.
- Preserve missing fields as `null`; do not manufacture identifiers or values.

### 3. Deterministic relationship resolver

Add a focused service, for example `app/v2_procurement_graph_service.py`.

Relationship rules:

- Exact PO number identifies a PO candidate.
- Exact B/L number identifies a shipment candidate.
- Exact contract number identifies a contract candidate.
- One contract with different explicit POs creates one parent case containing
  distinct child POs; it does not merge the POs.
- Different explicit PO or B/L IDs are hard conflicts and must never be
  automatically merged.
- Product, quantity, supplier, customer, ports, and dates are supporting
  evidence only.
- Quantity matching may create a review suggestion but must never establish
  identity automatically.

Candidate lookup must use indexed typed references before any contextual
comparison. Do not compare every new document with every historical document.

### 4. Human decisions

Keep the existing grouping states:

- `auto_grouped`
- `needs_review`
- `kept_ungrouped`
- `manually_assigned`

Manual associations receive `confirmed_by_review` status and must be preserved
on automatic rebuilds. A kept-ungrouped decision must continue to prevent
automatic re-assignment.

### 5. API

Keep current tender endpoints unchanged. Add:

- `GET /procurement-cases`
- `GET /procurement-cases/{case_id}`
- `GET /procurement-cases/{case_id}/relationships`
- `POST /procurement-cases/{case_id}/relationships/confirm`
- `POST /procurement-cases/{case_id}/relationships/reject`
- `GET /procurement-cases/{case_id}/discrepancies`

The upload-job UI should report these stages:

```text
Rendering → Classifying → Segmenting → Extracting → Resolving relationships → Review / Complete
```

## Frontend and UX Design

### Sidebar

Add **Cases** as the primary deal-management workspace.

- **Cases**: internal procurement deal hierarchy.
- **Tenders**: formal tender identifiers and the existing tender view.
- **Reviews**: unresolved extraction, relationship, and discrepancy decisions.

This avoids calling every grouped deal a tender.

### Cases list

Each row displays:

- Serial number
- Case display name
- Formal tender ID, if present
- Contract count
- PO count
- Shipment count
- Document count
- Open-review count
- Open-discrepancy count
- Last updated timestamp

Example:

```text
#  Case                                      Contract  POs  Shipments  Reviews
1  Mahakoshal / RKCB / 2024HFWX-1004         1         2    2          0–1
```

### Case detail page

Use a clear hierarchy rather than a dense graph:

```text
Case header
├─ Summary: customer, supplier, product, formal tender if any
├─ Contract section
├─ Purchase orders
│  ├─ Gudri: 220 MT, Gangavaram
│  │  └─ shipment and supporting documents
│  └─ Bhachau: 110 MT, Mundra
│     └─ shipment and supporting documents
├─ Shared documents and email threads
└─ Discrepancies and review decisions
```

Each item must show its exact reference, grouping reason, source PDF/page, and
status: automatic, confirmed by a user, or needs review.

### Document detail

Add a **Relationships** panel to the document view:

- Belongs to case
- References contract, PO, or shipment
- Evidence page
- Extraction confidence, clearly labelled as extraction reliability rather than
  business certainty
- Related components in the same PDF

### Reviews

Split reviews into clear categories:

- Missing or uncertain extraction
- Possible relationship
- Identity conflict
- Commercial discrepancy
- Logistics discrepancy

A price difference between a PO and invoice is a discrepancy to inspect, not a
reason to split otherwise well-supported document relationships.

## Delivery Sequence

1. Back up SQLite and add additive schema migrations only.
2. Create cases, entities, relationships, and read APIs without changing
   existing tender behavior.
3. Add PDF segment extraction and typed entity extraction.
4. Add the deterministic relationship resolver.
5. Build the Cases list and Case Detail UI.
6. Add review and discrepancy controls.
7. Run the Mahakoshal eight-file scenario as an integration test.
8. Backfill historical V2 documents cautiously, preserving human decisions and
   placing uncertainty into review.

## Acceptance Test: Mahakoshal

The completed system must produce:

- One Mahakoshal parent procurement case.
- One upstream contract: `2024HFWX-1004`.
- Two distinct POs: Gudri and Bhachau.
- Two distinct shipments: `JAM2412610` and `TMSE3279887`.
- Shared documents and correspondence linked to the parent case.
- No automatic merge of the two POs or shipments.
- Evidence pages and explanations visible for every automatic relationship.

## Additional Safeguards from the Ashok Minerals Scenario

The following requirements apply to all procurement cases. The identifiers and
expected values in the Ashok Minerals example below are proposed acceptance
expectations from a document analysis; they must be verified against the source
PDF pages before they are stored as confirmed facts.

### Negotiation history is not a confirmed transaction

An email thread can contain earlier proposals, quotations, negotiations,
amendments, cancellations, and superseded terms. Each commercial statement
must retain one of these statuses:

- `proposed`
- `quoted`
- `negotiated`
- `confirmed`
- `amended`
- `cancelled`
- `superseded`

The resolver must not create a contract, purchase order, shipment, or invoice
from a quantity or price merely because it appears in correspondence. A record
is confirmed only with explicit confirmation language, message chronology and
timestamps, or authoritative documents such as a signed contract, PO, B/L, or
invoice. The newest message is not automatically authoritative.

Historical commercial statements remain linked to their procurement case as
negotiation events. For example, an annual 264 MT proposal and a 44 MT proposed
order must not become additional POs when the executed documentation supports
only a 22 MT order.

### Preserve destination and date conflicts

City, region, and port descriptions are not interchangeable identities. A PO
may say Chennai while a contract, invoice, or B/L says Ennore or
Visakhapatnam. The resolver must not normalize those values into one location.

When the exact shipment identifier provides strong identity evidence, the
documents can remain linked to one shipment and a `logistics` discrepancy must
record every source value and its evidence page. A genuine contradiction must
remain visible; source values are never overwritten.

The same rule applies to dates. Extracted document dates are authoritative over
filename text. An apparent filename date conflict becomes a separate
discrepancy. Conflicting dates in two authoritative documents retain both
values and require review.

### Parties have distinct legal and commercial roles

The case layer must represent organizations independently from their roles.
One shipment can legitimately name an upstream seller, manufacturer/exporter,
intermediary buyer/reseller, final customer, consignee, and notify party. The
presence of two names in the same document is not evidence they are the same
organization.

Add a later additive entity layer for organizations and role assignments:

- `v2_organizations`: legal-name evidence and normalized name only when
  justified by source evidence.
- `v2_entity_party_roles`: entity, organization, role, source evidence, and
  status.

Roles such as seller, buyer, exporter, customer, consignee, and notify party
must remain distinct. This prevents an upstream supplier, physical shipper, and
customer from being silently merged.

### Ashok Minerals acceptance scenario

Use these five PDFs as a second integration scenario, without hardcoding their
names or values into the resolver:

1. `195-24 SC ELATE vs MINCHEM SINGAPORE.pdf`
2. `MINCHE~1.PDF`
3. `PO - MIN 001_24-25.pdf`
4. `PWD+190 BL-MEDUT1263630.pdf`
5. `1242500071_FINAL SD_MISPL_ASHOK MINERALS_NGF+190_22MT_12AUG2028_CIF ENNORE.pdf`

After source-page verification, the expected structure is one procurement case
with one upstream contract (`195/24`), one downstream customer PO
(`MIN-AME 001/24-25`), one shipment (`MEDUT1263630`), one upstream commercial
invoice (`335.GTE`), and one downstream commercial invoice (`1242500071`).
The expected confirmed product and quantity are Natural Graphite Flakes +190
and 22 MT. Any apparent upstream/downstream invoice values must remain separate
commercial facts; they are not a reason to split a correctly identified case.

The scenario must surface, rather than hide:

- destination wording conflicts;
- filename-year inconsistencies;
- certificate-of-origin reference differences; and
- earlier proposed quantities that were not part of the confirmed order.

## Expanded Test Requirements

The relationship resolver must pass both the Mahakoshal and Ashok Minerals
scenarios, plus negative tests where similarly named companies or products
must not be merged. Tests must assert both positive evidence-backed
relationships and the discrepancies/reviews that remain intentionally open.
