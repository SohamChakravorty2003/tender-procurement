import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent / "procurement.db"


@contextmanager
def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# Column list shared by fresh databases and migrations.
_BIDS_COLUMNS = """
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    tender_id         TEXT NOT NULL REFERENCES tenders(tender_id),
    email_id          TEXT,
    vendor_name       TEXT,
    vendor_email      TEXT,
    body_text         TEXT,
    attachment_names  TEXT,
    extracted_json    TEXT,
    content_hash      TEXT,
    extraction_status TEXT NOT NULL DEFAULT 'pending',
    created_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    document_id       INTEGER REFERENCES documents(id),
    offer_seq         INTEGER,
    message_seq       INTEGER,
    offered_at        TEXT,
    accepted          INTEGER NOT NULL DEFAULT 0,
    duplicate_of      INTEGER,
    CHECK (email_id IS NOT NULL OR document_id IS NOT NULL)
"""


def _migrate_existing_db(conn):
    """Bring old databases up to the current schema, including multi-offer Gmail bids."""
    tender_cols = {r["name"] for r in conn.execute("PRAGMA table_info(tenders)")}
    if "id_origin" not in tender_cols:
        conn.execute(
            "ALTER TABLE tenders ADD COLUMN id_origin TEXT NOT NULL "
            "DEFAULT 'extracted' CHECK (id_origin IN ('extracted','generated'))"
        )

    bid_cols = {r["name"]: r for r in conn.execute("PRAGMA table_info(bids)")}
    email_is_unique = False
    for index in conn.execute("PRAGMA index_list(bids)"):
        if index["unique"]:
            indexed_cols = [r["name"] for r in conn.execute(
                f"PRAGMA index_info('{index['name']}')"
            )]
            if indexed_cols == ["email_id"]:
                email_is_unique = True
                break

    if bid_cols["email_id"]["notnull"] or "document_id" not in bid_cols or email_is_unique:
        # Rebuild to remove the former one-email/one-bid unique constraint.
        old_cols = set(bid_cols)
        copy_cols = [name for name in (
            "id", "tender_id", "email_id", "vendor_name", "vendor_email", "body_text",
            "attachment_names", "extracted_json", "content_hash", "extraction_status",
            "created_at", "document_id", "offer_seq", "message_seq", "offered_at", "accepted", "duplicate_of",
        ) if name in old_cols]
        columns = ", ".join(copy_cols)
        conn.execute("DROP TABLE IF EXISTS bids_new")
        conn.execute(f"CREATE TABLE bids_new ({_BIDS_COLUMNS})")
        conn.execute(
            f"INSERT INTO bids_new ({columns}) SELECT {columns} FROM bids"
        )
        conn.execute("DROP TABLE bids")
        conn.execute("ALTER TABLE bids_new RENAME TO bids")

    email_cols = {r["name"] for r in conn.execute("PRAGMA table_info(email_analysis)")}
    if "attachment_context_hash" not in email_cols:
        conn.execute("ALTER TABLE email_analysis ADD COLUMN attachment_context_hash TEXT")
    if "attachments_checked_at" not in email_cols:
        conn.execute("ALTER TABLE email_analysis ADD COLUMN attachments_checked_at TEXT")
    
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(bids)")}
    if "accepted" not in cols:
        conn.execute("ALTER TABLE bids ADD COLUMN accepted INTEGER NOT NULL DEFAULT 0")
    if "duplicate_of" not in cols:
        conn.execute("ALTER TABLE bids ADD COLUMN duplicate_of INTEGER")
    if "message_seq" not in cols:
        conn.execute("ALTER TABLE bids ADD COLUMN message_seq INTEGER")

    # Preserve legacy duplicate pending rows, assigning them distinct sequences.
    # Existing rows remain intact; a successful extraction can later replace
    # redundant pending copies with the offers actually found in the message.
    used_by_email: dict[str, set[int]] = {}
    for row in conn.execute(
        "SELECT id, email_id, offer_seq FROM bids WHERE email_id IS NOT NULL "
        "ORDER BY email_id, offer_seq IS NULL, offer_seq, id"
    ):
        used = used_by_email.setdefault(row["email_id"], set())
        seq = row["offer_seq"]
        if seq is not None and seq not in used:
            used.add(seq)
            continue
        next_seq = max(used, default=0) + 1
        while next_seq in used:
            next_seq += 1
        conn.execute("UPDATE bids SET offer_seq = ? WHERE id = ?", (next_seq, row["id"]))
        used.add(next_seq)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_bids_tender ON bids(tender_id)")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_bids_doc_offer "
        "ON bids(document_id, offer_seq) WHERE document_id IS NOT NULL"
    )
    # Treat a missing legacy sequence as 1 for idempotence even if an older
    # running process attempts to insert a row without offer_seq.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_bids_email_offer "
        "ON bids(email_id, COALESCE(offer_seq, 1)) WHERE email_id IS NOT NULL"
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS deleted_emails (
            email_id TEXT PRIMARY KEY,
            thread_id TEXT,
            tender_id TEXT,
            deleted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    deleted_email_cols = {r["name"] for r in conn.execute("PRAGMA table_info(deleted_emails)")}
    if "thread_id" not in deleted_email_cols:
        conn.execute("ALTER TABLE deleted_emails ADD COLUMN thread_id TEXT")
    if "tender_id" not in deleted_email_cols:
        conn.execute("ALTER TABLE deleted_emails ADD COLUMN tender_id TEXT")

    document_cols = {r["name"] for r in conn.execute("PRAGMA table_info(documents)")}
    if "offer_extraction_status" not in document_cols:
        conn.execute(
            "ALTER TABLE documents ADD COLUMN offer_extraction_status TEXT NOT NULL "
            "DEFAULT 'pending' CHECK (offer_extraction_status IN "
            "('pending','extracted','no_offers','failed'))"
        )
    conn.execute(
        "UPDATE documents SET offer_extraction_status = 'extracted' "
        "WHERE document_type IN ('email_thread', 'quotation') AND offer_extraction_status = 'pending' "
        "AND EXISTS (SELECT 1 FROM bids b WHERE b.document_id = documents.id)"
    )

    v2_document_cols = {r["name"] for r in conn.execute("PRAGMA table_info(v2_documents)")}
    if "upload_batch_id" not in v2_document_cols:
        conn.execute("ALTER TABLE v2_documents ADD COLUMN upload_batch_id TEXT")
    grouping_state_added = "grouping_state" not in v2_document_cols
    if grouping_state_added:
        conn.execute(
            "ALTER TABLE v2_documents ADD COLUMN grouping_state TEXT NOT NULL DEFAULT 'needs_review' "
            "CHECK (grouping_state IN ('auto_grouped', 'needs_review', 'kept_ungrouped', 'manually_assigned'))"
        )
    v2_job_cols = {r["name"] for r in conn.execute("PRAGMA table_info(v2_processing_jobs)")}
    if "stage" not in v2_job_cols:
        conn.execute("ALTER TABLE v2_processing_jobs ADD COLUMN stage TEXT NOT NULL DEFAULT 'queued'")
    if "current_page" not in v2_job_cols:
        conn.execute("ALTER TABLE v2_processing_jobs ADD COLUMN current_page INTEGER")
    if "total_pages" not in v2_job_cols:
        conn.execute("ALTER TABLE v2_processing_jobs ADD COLUMN total_pages INTEGER")
    v2_reference_cols = {r["name"] for r in conn.execute("PRAGMA table_info(v2_references)")}
    if "confidence" not in v2_reference_cols:
        conn.execute("ALTER TABLE v2_references ADD COLUMN confidence REAL")
    if grouping_state_added:
        # Derive a conservative state for records created before grouping state
        # existed. Manual choices take precedence over automatic associations.
        conn.execute(
            """UPDATE v2_documents SET grouping_state = 'auto_grouped'
               WHERE EXISTS (
                   SELECT 1 FROM v2_tender_associations a
                   WHERE a.document_id = v2_documents.id AND a.status = 'confirmed'
               )"""
        )
        conn.execute(
            """UPDATE v2_documents SET grouping_state = 'manually_assigned'
               WHERE EXISTS (
                   SELECT 1 FROM v2_tender_associations a
                   WHERE a.document_id = v2_documents.id AND a.status = 'confirmed'
                     AND a.method = 'manual_review'
               )"""
        )
        conn.execute(
            """UPDATE v2_documents SET grouping_state = 'kept_ungrouped'
               WHERE NOT EXISTS (
                   SELECT 1 FROM v2_tender_associations a
                   WHERE a.document_id = v2_documents.id AND a.status = 'confirmed'
               ) AND EXISTS (
                   SELECT 1 FROM v2_review_cases r
                   WHERE r.document_id = v2_documents.id AND r.status = 'rejected'
                     AND r.review_type IN ('no_tender_id', 'conflicting_reference', 'multiple_tender_ids', 'conflicting_upload_batch_tender')
               )"""
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_v2_documents_upload_batch ON v2_documents(upload_batch_id)"
    )


def init_db():
    with get_connection() as conn:
        conn.executescript(f"""
            CREATE TABLE IF NOT EXISTS tenders (
                tender_id        TEXT PRIMARY KEY,
                title            TEXT,
                requirements_json TEXT,
                source_email_id  TEXT,
                id_origin        TEXT NOT NULL DEFAULT 'extracted'
                                 CHECK (id_origin IN ('extracted','generated')),
                created_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS email_analysis (
                email_id         TEXT PRIMARY KEY,
                thread_id        TEXT,
                sender           TEXT,
                subject          TEXT,
                category         TEXT,
                email_role       TEXT,
                tender_id        TEXT REFERENCES tenders(tender_id),
                tender_id_source TEXT,
                confidence       REAL,
                status           TEXT NOT NULL DEFAULT 'ok',
                attachment_context_hash TEXT,
                attachments_checked_at TEXT,
                analyzed_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_analysis_tender
                ON email_analysis(tender_id);
            CREATE INDEX IF NOT EXISTS idx_analysis_thread
                ON email_analysis(thread_id);

            CREATE TABLE IF NOT EXISTS bids ({_BIDS_COLUMNS});

            CREATE TABLE IF NOT EXISTS documents (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                source              TEXT NOT NULL CHECK (source IN ('upload','gmail')),
                file_name           TEXT NOT NULL,
                sha256              TEXT NOT NULL UNIQUE,
                email_id            TEXT,
                gmail_attachment_id TEXT,
                page_count          INTEGER,
                full_text           TEXT,
                page_quality_json   TEXT,
                extraction_status   TEXT NOT NULL DEFAULT 'pending',
                offer_extraction_status TEXT NOT NULL DEFAULT 'pending'
                                     CHECK (offer_extraction_status IN ('pending','extracted','no_offers','failed')),
                document_type       TEXT,
                contains_json       TEXT,
                analysis_json       TEXT,
                tender_id           TEXT REFERENCES tenders(tender_id),
                link_method         TEXT,
                created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_documents_tender ON documents(tender_id);
            CREATE INDEX IF NOT EXISTS idx_documents_email ON documents(email_id);

            CREATE TABLE IF NOT EXISTS tender_references (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                tender_id   TEXT REFERENCES tenders(tender_id),
                ref_type    TEXT NOT NULL,
                ref_value   TEXT NOT NULL,
                source_kind TEXT NOT NULL CHECK (source_kind IN ('email','document')),
                source_id   TEXT NOT NULL,
                created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (ref_type, ref_value, source_kind, source_id)
            );
            CREATE INDEX IF NOT EXISTS idx_refs_value
                ON tender_references(ref_type, ref_value);
            CREATE INDEX IF NOT EXISTS idx_refs_tender ON tender_references(tender_id);

            CREATE TABLE IF NOT EXISTS link_reviews (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                review_type        TEXT NOT NULL CHECK (review_type IN ('conflict','suggested')),
                source_kind        TEXT NOT NULL CHECK (source_kind IN ('email','document')),
                source_id          TEXT NOT NULL,
                tender_id          TEXT NOT NULL REFERENCES tenders(tender_id),
                other_tender_id    TEXT REFERENCES tenders(tender_id),
                reason             TEXT,
                status             TEXT NOT NULL DEFAULT 'pending'
                                   CHECK (status IN ('pending','confirmed','rejected')),
                resolved_tender_id TEXT REFERENCES tenders(tender_id),
                created_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                resolved_at        TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_reviews_status ON link_reviews(status);

            CREATE TABLE IF NOT EXISTS v2_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_name TEXT NOT NULL,
                sha256 TEXT NOT NULL UNIQUE,
                source_pdf BLOB NOT NULL,
                upload_batch_id TEXT,
                grouping_state TEXT NOT NULL DEFAULT 'needs_review'
                    CHECK (grouping_state IN ('auto_grouped', 'needs_review', 'kept_ungrouped', 'manually_assigned')),
                page_count INTEGER,
                document_type TEXT,
                processing_state TEXT NOT NULL DEFAULT 'queued',
                analysis_json TEXT,
                error_text TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS v2_processing_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL REFERENCES v2_documents(id) ON DELETE CASCADE,
                status TEXT NOT NULL DEFAULT 'queued',
                stage TEXT NOT NULL DEFAULT 'queued',
                current_page INTEGER,
                total_pages INTEGER,
                attempts INTEGER NOT NULL DEFAULT 0,
                error_text TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                started_at TEXT,
                completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS v2_upload_batches (
                batch_id TEXT PRIMARY KEY,
                sealed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS v2_document_pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL REFERENCES v2_documents(id) ON DELETE CASCADE,
                page_number INTEGER NOT NULL,
                image_sha256 TEXT NOT NULL,
                width_px INTEGER NOT NULL,
                height_px INTEGER NOT NULL,
                mime_type TEXT NOT NULL,
                UNIQUE(document_id, page_number)
            );
            CREATE TABLE IF NOT EXISTS v2_extraction_windows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL REFERENCES v2_documents(id) ON DELETE CASCADE,
                page_numbers_json TEXT NOT NULL,
                detail_json TEXT NOT NULL,
                negotiation_events_json TEXT,
                UNIQUE(document_id, page_numbers_json)
            );
            CREATE TABLE IF NOT EXISTS v2_references (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL REFERENCES v2_documents(id) ON DELETE CASCADE,
                reference_type TEXT NOT NULL,
                raw_value TEXT NOT NULL,
                normalized_value TEXT NOT NULL,
                confidence REAL,
                evidence_json TEXT NOT NULL,
                UNIQUE(document_id, reference_type, raw_value)
            );
            CREATE TABLE IF NOT EXISTS v2_tender_associations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL REFERENCES v2_documents(id) ON DELETE CASCADE,
                tender_id TEXT NOT NULL REFERENCES tenders(tender_id),
                method TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                UNIQUE(document_id, tender_id)
            );
            CREATE TABLE IF NOT EXISTS v2_review_cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER REFERENCES v2_documents(id) ON DELETE CASCADE,
                review_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                resolved_at TEXT
            );
            CREATE TABLE IF NOT EXISTS v2_derived_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL REFERENCES v2_documents(id) ON DELETE CASCADE,
                record_kind TEXT NOT NULL,
                source_window_id INTEGER REFERENCES v2_extraction_windows(id) ON DELETE CASCADE,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v2_email_threads (
                thread_id TEXT PRIMARY KEY,
                tender_id TEXT REFERENCES tenders(tender_id),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS v2_email_messages (
                email_id TEXT PRIMARY KEY,
                thread_id TEXT REFERENCES v2_email_threads(thread_id),
                sender TEXT,
                recipient TEXT,
                subject TEXT,
                body TEXT,
                category TEXT,
                email_role TEXT,
                tender_id TEXT REFERENCES tenders(tender_id),
                observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_v2_jobs_status ON v2_processing_jobs(status);
            CREATE INDEX IF NOT EXISTS idx_v2_docs_state ON v2_documents(processing_state);
            CREATE INDEX IF NOT EXISTS idx_v2_refs_value ON v2_references(reference_type, normalized_value);
            CREATE INDEX IF NOT EXISTS idx_v2_associations_tender ON v2_tender_associations(tender_id);
        """)
        _migrate_existing_db(conn)


def normalize_tender_id(tender_id: str | None) -> str | None:
    if not tender_id or not tender_id.strip():
        return None
    return tender_id.strip().upper()


def _normalize_v2_reference(reference_type: str, value: str) -> str:
    """Normalise a typed printed reference conservatively for equality lookup only."""
    normalized = re.sub(r"[^A-Z0-9]+", "", value.upper())
    prefixes = {
        "po_number": ("PURCHASEORDERNO", "PURCHASEORDER", "PONO", "PO"),
        "contract_number": ("SALESCONTRACTNO", "SALESCONTRACT", "CONTRACTNO", "CONTRACT"),
    }
    for prefix in prefixes.get(reference_type, ()):
        if normalized.startswith(prefix) and len(normalized) > len(prefix):
            return normalized[len(prefix):]
    return normalized


def _reference_confidence(reference: dict) -> float | None:
    """Return a validated optional visual-confidence value for display only."""
    confidence = reference.get("confidence")
    if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
        return float(confidence) if 0 <= confidence <= 1 else None
    return None


def create_v2_document_job(
    file_name: str,
    pdf_bytes: bytes,
    sha256: str,
    *,
    upload_batch_id: str | None = None,
) -> dict:
    """Persist one V2 source PDF and its optional user-selected upload set."""
    with get_connection() as conn:
        document = conn.execute("SELECT id, processing_state FROM v2_documents WHERE sha256 = ?", (sha256,)).fetchone()
        if document:
            job = conn.execute(
                "SELECT * FROM v2_processing_jobs WHERE document_id = ? ORDER BY id DESC LIMIT 1", (document["id"],)
            ).fetchone()
            return {"document_id": document["id"], "job_id": job["id"], "status": job["status"], "is_new": False}
        cursor = conn.execute(
            "INSERT INTO v2_documents (file_name, sha256, source_pdf, upload_batch_id) VALUES (?, ?, ?, ?)",
            (file_name, sha256, pdf_bytes, upload_batch_id),
        )
        if upload_batch_id:
            conn.execute("INSERT OR IGNORE INTO v2_upload_batches (batch_id) VALUES (?)", (upload_batch_id,))
        document_id = cursor.lastrowid
        job_id = conn.execute("INSERT INTO v2_processing_jobs (document_id) VALUES (?)", (document_id,)).lastrowid
    return {"document_id": document_id, "job_id": job_id, "status": "queued", "is_new": True}


def claim_v2_job(job_id: int) -> tuple[dict, bytes] | None:
    """Atomically claim a queued V2 job and return its stored PDF bytes."""
    with get_connection() as conn:
        job = conn.execute(
            "SELECT j.*, d.source_pdf FROM v2_processing_jobs j JOIN v2_documents d ON d.id = j.document_id WHERE j.id = ?",
            (job_id,),
        ).fetchone()
        if job is None or job["status"] != "queued":
            return None
        changed = conn.execute(
            "UPDATE v2_processing_jobs SET status = 'running', stage = 'rendering', attempts = attempts + 1, started_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'queued'",
            (job_id,),
        ).rowcount
        if not changed:
            return None
        conn.execute("UPDATE v2_documents SET processing_state = 'analyzing', error_text = NULL WHERE id = ?", (job["document_id"],))
        item = dict(job)
    return item, item.pop("source_pdf")


def update_v2_job_progress(
    job_id: int, stage: str, *, current_page: int | None = None, total_pages: int | None = None
) -> None:
    """Persist coarse, user-visible V2 worker progress without storing document content."""
    if stage not in {"rendering", "routing", "classifying", "extracting", "persisting", "grouping"}:
        raise ValueError(f"unsupported V2 job stage: {stage}")
    with get_connection() as conn:
        conn.execute(
            """UPDATE v2_processing_jobs
               SET stage = ?, current_page = ?, total_pages = COALESCE(?, total_pages)
               WHERE id = ? AND status = 'running'""",
            (stage, current_page, total_pages, job_id),
        )


def _v2_insert_review(conn, document_id: int, review_type: str, payload: dict) -> None:
    """Create one pending V2 review per document and issue type."""
    existing = conn.execute(
        "SELECT 1 FROM v2_review_cases WHERE document_id = ? AND review_type = ? AND status = 'pending'",
        (document_id, review_type),
    ).fetchone()
    if existing:
        return
    conn.execute(
        "INSERT INTO v2_review_cases (document_id, review_type, payload_json) VALUES (?, ?, ?)",
        (document_id, review_type, json.dumps(payload)),
    )


def _v2_result_references(result: dict) -> list[dict]:
    """Collect typed references from classification plus evidence-backed detail output."""
    classification = result["classification"]
    references = [*classification.get("tender_ids", []), *classification.get("references", [])]
    document_type = classification["document_type"]
    for window in result.get("detail_windows", []):
        detail = window.get("detail") or {}
        references.extend(detail.get("references", []))
        evidence = detail.get("evidence") or []
        if document_type == "email_thread":
            for message in detail.get("messages", []):
                references.extend(message.get("references", []))
                references.extend(_v2_labelled_thread_references(message))
        if document_type == "purchase_order" and detail.get("po_number"):
            references.append({"reference_type": "po_number", "raw_value": detail["po_number"], "evidence": evidence})
        elif document_type == "sales_contract" and detail.get("contract_number"):
            references.append({"reference_type": "contract_number", "raw_value": detail["contract_number"], "evidence": evidence})
        elif document_type == "shipping_document":
            for component in detail.get("components", []):
                component_evidence = component.get("evidence") or evidence
                if component.get("bill_of_lading_number"):
                    references.append({"reference_type": "bill_of_lading_number", "raw_value": component["bill_of_lading_number"], "evidence": component_evidence})
                if component.get("invoice_number"):
                    references.append({"reference_type": "invoice_number", "raw_value": component["invoice_number"], "evidence": component_evidence})
    unique: dict[tuple[str, str], dict] = {}
    non_tender_units = {"USD/MT", "USD/TON", "USD/TONNE", "USD/KG", "USD/CBM", "G/CM3", "G/CC"}
    for reference in references:
        if reference.get("reference_type") and reference.get("raw_value"):
            if (
                reference["reference_type"] == "tender_id"
                and re.sub(r"\s+", "", str(reference["raw_value"]).upper()) in non_tender_units
            ):
                continue
            unique[(reference["reference_type"], reference["raw_value"])] = reference
    return list(unique.values())


_V2_THREAD_REFERENCE_PATTERNS = (
    ("tender_id", re.compile(r"\(([A-Z][A-Z0-9]*(?:[_/][A-Z0-9]+)+)\)", re.IGNORECASE)),
    ("tender_id", re.compile(r"\b(?:enquiry|tender|rfq|reference)\s*(?:(?:no|number|id)\.?\s*)?[:#-]?\s*([A-Z][A-Z0-9]*(?:[_/-][A-Z0-9]+)+|[A-Z]{2,}[0-9]{3,})\b", re.IGNORECASE)),
    ("contract_number", re.compile(r"\b(?:signed\s+)?contract\s*(?:(?:no|number)\.?|n[°ºo.]?)\s*[:#]?\s*([A-Z0-9][A-Z0-9/-]{3,})", re.IGNORECASE)),
    ("po_number", re.compile(r"\b(?:purchase\s+order|po)\s*(?:(?:no|number)\.?\s*[:#]?|[:#])\s*([A-Z0-9][A-Z0-9/-]{3,})", re.IGNORECASE)),
    ("bill_of_lading_number", re.compile(r"\b(?:bill\s+of\s+lading|b/?l)\s*(?:(?:no|number)\.?\s*[:#]?|[:#])\s*([A-Z0-9][A-Z0-9/-]{3,})", re.IGNORECASE)),
    ("invoice_number", re.compile(r"\b(?:commercial\s+)?invoice\s*(?:(?:no|number)\.?\s*[:#]?|[:#])\s*([A-Z0-9][A-Z0-9/-]{3,})", re.IGNORECASE)),
)


def _v2_labelled_thread_references(message: dict) -> list[dict]:
    """Derive typed references from explicitly labelled saved thread text only."""
    message_index = message.get("message_index")
    source_items = []
    if message.get("subject_raw"):
        source_items.append((message["subject_raw"], message.get("start_page")))
    for item in message.get("body_evidence", []):
        source_items.append((item.get("evidence_text"), item.get("page_number")))

    extracted: list[dict] = []
    for text, page_number in source_items:
        if not isinstance(text, str) or not isinstance(page_number, int):
            continue
        for reference_type, pattern in _V2_THREAD_REFERENCE_PATTERNS:
            for match in pattern.finditer(text):
                extracted.append({
                    "reference_type": reference_type,
                    "raw_value": match.group(1),
                    "evidence": [{
                        "page_number": page_number,
                        "evidence_text": match.group(0),
                        "message_index": message_index,
                    }],
                })
    return extracted


_V2_TO_LEGACY_REFERENCE_TYPES = {
    "tender_id": "tender_id",
    "po_number": "po_number",
    "contract_number": "contract_number",
    "bill_of_lading_number": "bl_number",
    "invoice_number": "invoice_number",
}


def _v2_reference_candidates(conn, references: list[tuple[str, str]], *, exclude_document_id: int) -> set[str]:
    """Return confirmed tender candidates sharing compatible typed references."""
    candidates: set[str] = set()
    for reference_type, normalized_value in references:
        rows = conn.execute(
            """SELECT DISTINCT a.tender_id FROM v2_references r
               JOIN v2_tender_associations a ON a.document_id = r.document_id
               WHERE r.reference_type = ? AND r.normalized_value = ?
                 AND r.document_id != ? AND a.status = 'confirmed'""",
            (reference_type, normalized_value, exclude_document_id),
        ).fetchall()
        candidates.update(row["tender_id"] for row in rows)
        legacy_type = _V2_TO_LEGACY_REFERENCE_TYPES.get(reference_type)
        if legacy_type:
            rows = conn.execute(
                "SELECT DISTINCT tender_id FROM tender_references WHERE ref_type = ? AND ref_value = ? AND tender_id IS NOT NULL",
                (legacy_type, normalized_value),
            ).fetchall()
            candidates.update(row["tender_id"] for row in rows)
    return candidates


def _v2_cross_type_reference_candidates(conn, references: list[tuple[str, str]], *, exclude_document_ids: set[int]) -> set[str]:
    """Return tender candidates sharing a value under a different reference type.

    Matching a PO number to a contract number is never an automatic association.
    It is retained as a review signal so two generated tenders are not created
    around the same ambiguous printed identifier.
    """
    candidates: set[str] = set()
    if not references:
        return candidates
    placeholders = ", ".join("?" for _ in exclude_document_ids)
    for reference_type, normalized_value in references:
        rows = conn.execute(
            f"""SELECT DISTINCT a.tender_id FROM v2_references r
                JOIN v2_tender_associations a ON a.document_id = r.document_id
                WHERE r.normalized_value = ? AND r.reference_type != ?
                  AND r.document_id NOT IN ({placeholders}) AND a.status = 'confirmed'""",
            (normalized_value, reference_type, *exclude_document_ids),
        ).fetchall()
        candidates.update(row["tender_id"] for row in rows)
    return candidates


def _v2_safe_contract_invoice_bridge_candidate(
    conn,
    references: list[tuple[str, str]],
    *,
    exclude_document_ids: set[int],
) -> str | None:
    """Return one safe contract/invoice bridge tender, otherwise ``None``.

    Some counterparties deliberately reuse a structured commercial identifier
    on a contract and its invoice. This is narrower than generic cross-type
    matching: only an exact, alphanumeric contract/invoice value can bridge,
    and only when all supporting documents point to exactly one tender. Purely
    numeric values and every other type pair remain review-only.
    """
    if not references:
        return None
    placeholders = ", ".join("?" for _ in exclude_document_ids)
    candidates: set[str] = set()
    for reference_type, normalized_value in references:
        if reference_type not in {"contract_number", "invoice_number"}:
            continue
        if not (len(normalized_value) >= 5 and any(char.isalpha() for char in normalized_value)
                and any(char.isdigit() for char in normalized_value)):
            continue
        counterpart_type = (
            "invoice_number" if reference_type == "contract_number" else "contract_number"
        )
        rows = conn.execute(
            f"""SELECT DISTINCT a.tender_id FROM v2_references r
                JOIN v2_tender_associations a ON a.document_id = r.document_id
                WHERE r.reference_type = ?
                  AND r.normalized_value = ?
                  AND r.document_id NOT IN ({placeholders})
                  AND a.status = 'confirmed'""",
            (counterpart_type, normalized_value, *exclude_document_ids),
        ).fetchall()
        candidates.update(row["tender_id"] for row in rows)
    return next(iter(candidates)) if len(candidates) == 1 else None


def _next_generated_tender_id(conn) -> str:
    """Return the next repository-standard generated tender id within one transaction."""
    rows = conn.execute("SELECT tender_id FROM tenders WHERE tender_id GLOB 'TND-[0-9]*'").fetchall()
    numbers = [int(row["tender_id"][4:]) for row in rows if row["tender_id"][4:].isdigit()]
    return f"TND-{max(numbers, default=0) + 1:04d}"


def _v2_reference_group_rows(conn) -> list[dict]:
    """Return completed V2 documents and their valid typed reference edges."""
    rows = conn.execute(
        """SELECT r.document_id, r.reference_type, r.normalized_value
           FROM v2_references r JOIN v2_documents d ON d.id = r.document_id
           WHERE d.processing_state = 'completed'"""
    ).fetchall()
    return [dict(row) for row in rows if len(row["normalized_value"]) >= 5 and any(char.isdigit() for char in row["normalized_value"])]


def rebuild_v2_reference_grouping(document_ids: list[int] | None = None) -> dict:
    """Rebuild saved V2 references and safely associate unique typed-reference groups.

    Existing confirmed associations are retained. A group with no candidate tender
    creates one generated tender; a group with one candidate attaches to it; two
    or more candidates create review cases without any automatic merge.
    """
    with get_connection() as conn:
        sql = "SELECT id, analysis_json, grouping_state FROM v2_documents WHERE processing_state = 'completed'"
        params: tuple[object, ...] = ()
        if document_ids is not None:
            if not document_ids:
                return {"documents_rebuilt": 0, "tenders_created": [], "documents_attached": [], "reviews_created": []}
            placeholders = ", ".join("?" for _ in document_ids)
            sql += f" AND id IN ({placeholders})"
            params = tuple(document_ids)
        documents = conn.execute(sql, params).fetchall()
        target_ids = [row["id"] for row in documents]
        automatic_target_ids = [
            row["id"] for row in documents
            if row["grouping_state"] not in {"kept_ungrouped", "manually_assigned"}
        ]
        if not target_ids:
            return {"documents_rebuilt": 0, "tenders_created": [], "documents_attached": [], "reviews_created": []}

        placeholders = ", ".join("?" for _ in target_ids)
        conn.execute(f"DELETE FROM v2_references WHERE document_id IN ({placeholders})", target_ids)
        conn.execute(
            f"DELETE FROM v2_review_cases WHERE document_id IN ({placeholders}) "
            "AND review_type IN ('no_tender_id', 'conflicting_reference') AND status = 'pending'",
            target_ids,
        )
        for document in documents:
            result = json.loads(document["analysis_json"] or "null")
            if not result:
                continue
            for reference in _v2_result_references(result):
                conn.execute(
                    "INSERT OR IGNORE INTO v2_references (document_id, reference_type, raw_value, normalized_value, confidence, evidence_json) VALUES (?, ?, ?, ?, ?, ?)",
                    (document["id"], reference["reference_type"], reference["raw_value"], _normalize_v2_reference(reference["reference_type"], reference["raw_value"]), _reference_confidence(reference), json.dumps(reference["evidence"])),
                )

        for document_id in automatic_target_ids:
            rows = conn.execute(
                "SELECT raw_value FROM v2_references WHERE document_id = ? AND reference_type = 'tender_id'",
                (document_id,),
            ).fetchall()
            tender_ids = sorted({normalize_tender_id(row["raw_value"]) for row in rows if normalize_tender_id(row["raw_value"])})
            if len(tender_ids) == 1:
                tender_id = tender_ids[0]
                conn.execute("INSERT OR IGNORE INTO tenders (tender_id, id_origin) VALUES (?, 'extracted')", (tender_id,))
                conn.execute(
                    "INSERT OR IGNORE INTO v2_tender_associations (document_id, tender_id, method, status) VALUES (?, ?, 'explicit', 'confirmed')",
                    (document_id, tender_id),
                )
                conn.execute("UPDATE v2_documents SET grouping_state = 'auto_grouped' WHERE id = ?", (document_id,))
            elif len(tender_ids) > 1:
                _v2_insert_review(conn, document_id, "multiple_tender_ids", {"tender_ids": tender_ids})

        reference_rows = _v2_reference_group_rows(conn)
        by_reference: dict[tuple[str, str], set[int]] = {}
        for row in reference_rows:
            by_reference.setdefault((row["reference_type"], row["normalized_value"]), set()).add(row["document_id"])
        neighbors: dict[int, set[int]] = {}
        for members in by_reference.values():
            if len(members) < 2:
                continue
            for document_id in members:
                neighbors.setdefault(document_id, set()).update(members - {document_id})

        visited: set[int] = set()
        tenders_created: list[str] = []
        documents_attached: list[int] = []
        reviews_created: list[int] = []
        for seed in sorted(neighbors):
            association_method: str | None = None
            if seed in visited:
                continue
            component: set[int] = set()
            pending = [seed]
            while pending:
                document_id = pending.pop()
                if document_id in component:
                    continue
                component.add(document_id)
                pending.extend(neighbors.get(document_id, set()) - component)
            visited.update(component)
            reference_pairs = [(row["reference_type"], row["normalized_value"]) for row in reference_rows if row["document_id"] in component]
            candidates: set[str] = set()
            for document_id in component:
                rows = conn.execute("SELECT tender_id FROM v2_tender_associations WHERE document_id = ? AND status = 'confirmed'", (document_id,)).fetchall()
                candidates.update(row["tender_id"] for row in rows)
            for document_id in component:
                candidates.update(_v2_reference_candidates(conn, [pair for pair in reference_pairs], exclude_document_id=document_id))
            cross_type_candidates = _v2_cross_type_reference_candidates(
                conn, reference_pairs, exclude_document_ids=component,
            )
            bridge_tender_id = _v2_safe_contract_invoice_bridge_candidate(
                conn, reference_pairs, exclude_document_ids=component,
            )

            unassociated = []
            for document_id in component:
                grouping_state = conn.execute(
                    "SELECT grouping_state FROM v2_documents WHERE id = ?", (document_id,)
                ).fetchone()["grouping_state"]
                if grouping_state in {"kept_ungrouped", "manually_assigned"}:
                    continue
                if not conn.execute(
                    "SELECT 1 FROM v2_tender_associations WHERE document_id = ? AND status = 'confirmed'",
                    (document_id,),
                ).fetchone():
                    unassociated.append(document_id)
            if not unassociated:
                continue
            if len(candidates) == 0 and bridge_tender_id:
                candidates = {bridge_tender_id}
                association_method = "contract_invoice_bridge"
            elif len(candidates) == 0 and cross_type_candidates:
                for document_id in unassociated:
                    _v2_insert_review(conn, document_id, "conflicting_reference", {"reference_candidates": sorted(cross_type_candidates), "component_document_ids": sorted(component), "reason": "same value has a different typed reference"})
                    reviews_created.append(document_id)
                continue
            if len(candidates) == 0:
                tender_id = _next_generated_tender_id(conn)
                conn.execute("INSERT INTO tenders (tender_id, id_origin) VALUES (?, 'generated')", (tender_id,))
                tenders_created.append(tender_id)
                association_method = "generated_typed_reference"
            elif len(candidates) == 1:
                tender_id = next(iter(candidates))
                association_method = association_method or "typed_reference"
            else:
                for document_id in unassociated:
                    _v2_insert_review(conn, document_id, "conflicting_reference", {"reference_candidates": sorted(candidates), "component_document_ids": sorted(component)})
                    reviews_created.append(document_id)
                continue
            for document_id in unassociated:
                _v2_attach_by_reference(conn, document_id, tender_id, method=association_method)
                conn.execute("UPDATE v2_documents SET grouping_state = 'auto_grouped' WHERE id = ?", (document_id,))
                documents_attached.append(document_id)
            component_ids = tuple(component)
            component_placeholders = ", ".join("?" for _ in component_ids)
            conn.execute(
                f"DELETE FROM v2_review_cases WHERE document_id IN ({component_placeholders}) "
                "AND review_type = 'no_tender_id' AND status = 'pending'",
                component_ids,
            )

        for document_id in automatic_target_ids:
            if not conn.execute("SELECT 1 FROM v2_tender_associations WHERE document_id = ? AND status = 'confirmed'", (document_id,)).fetchone():
                rows = conn.execute("SELECT reference_type, normalized_value FROM v2_references WHERE document_id = ?", (document_id,)).fetchall()
                reference_pairs = [(row["reference_type"], row["normalized_value"]) for row in rows]
                cross_type_candidates = _v2_cross_type_reference_candidates(
                    conn, reference_pairs, exclude_document_ids={document_id},
                )
                bridge_tender_id = _v2_safe_contract_invoice_bridge_candidate(
                    conn, reference_pairs, exclude_document_ids={document_id},
                )
                if bridge_tender_id:
                    _v2_attach_by_reference(conn, document_id, bridge_tender_id, method="contract_invoice_bridge")
                    conn.execute("UPDATE v2_documents SET grouping_state = 'auto_grouped' WHERE id = ?", (document_id,))
                elif cross_type_candidates:
                    _v2_insert_review(conn, document_id, "conflicting_reference", {"reference_candidates": sorted(cross_type_candidates), "reason": "same value has a different typed reference"})
                else:
                    _v2_insert_review(conn, document_id, "no_tender_id", {"grouping": "no unique shared typed reference"})
                conn.execute("UPDATE v2_documents SET grouping_state = 'needs_review' WHERE id = ?", (document_id,))
                reviews_created.append(document_id)
        # A mixed upload batch is deliberately flagged as a whole. Once a
        # document is independently anchored by its own explicit tender ID or
        # by a unique typed-reference match, that batch-level warning is no
        # longer actionable for this document. Keep it for generated and
        # unassociated documents, where a human decision may still be needed.
        conn.execute(
            f"""UPDATE v2_review_cases
                SET status = 'resolved', resolved_at = CURRENT_TIMESTAMP
                WHERE document_id IN ({placeholders})
                  AND review_type = 'conflicting_upload_batch_tender'
                  AND status = 'pending'
                  AND EXISTS (
                      SELECT 1 FROM v2_tender_associations a
                      WHERE a.document_id = v2_review_cases.document_id
                        AND a.status = 'confirmed'
                        AND a.method IN ('explicit', 'typed_reference')
                  )""",
            target_ids,
        )
    return {"documents_rebuilt": len(target_ids), "tenders_created": tenders_created, "documents_attached": sorted(documents_attached), "reviews_created": sorted(reviews_created)}


def _v2_tender_signature(value: str | None) -> str | None:
    """Compare tender-id spellings without changing the stored tender identifier."""
    tender_id = normalize_tender_id(value)
    return re.sub(r"[^A-Z0-9]", "", tender_id) if tender_id else None


def reconcile_v2_upload_batch(document_id: int) -> dict:
    """Check a completed upload set without treating selection as tender evidence.

    PDF selection is a convenience for job tracking, not proof that every file
    belongs to one deal. Document associations are therefore made only by the
    explicit-ID and typed-reference component logic in
    :func:`rebuild_v2_reference_grouping`.
    """
    with get_connection() as conn:
        source = conn.execute(
            "SELECT upload_batch_id FROM v2_documents WHERE id = ?", (document_id,)
        ).fetchone()
        batch_id = source["upload_batch_id"] if source else None
        if not batch_id:
            return {"status": "not_batched"}
        batch = conn.execute(
            "SELECT sealed_at FROM v2_upload_batches WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        if batch is None or batch["sealed_at"] is None:
            return {"status": "waiting_for_upload_complete", "upload_batch_id": batch_id}

        states = conn.execute(
            """SELECT d.id, d.processing_state, d.grouping_state, j.status
               FROM v2_documents d JOIN v2_processing_jobs j ON j.document_id = d.id
               WHERE d.upload_batch_id = ?""",
            (batch_id,),
        ).fetchall()
        if any(row["status"] in {"queued", "running"} for row in states):
            return {"status": "waiting_for_batch", "upload_batch_id": batch_id}
        completed_ids = [
            row["id"] for row in states
            if row["status"] == "completed" and row["processing_state"] == "completed"
            and row["grouping_state"] not in {"kept_ungrouped", "manually_assigned"}
        ]
        if not completed_ids:
            return {"status": "no_automatically_groupable_documents", "upload_batch_id": batch_id}

        placeholders = ", ".join("?" for _ in completed_ids)
        tender_rows = conn.execute(
            f"""SELECT document_id, raw_value FROM v2_references
                WHERE document_id IN ({placeholders}) AND reference_type = 'tender_id'""",
            completed_ids,
        ).fetchall()
        tender_ids_by_signature: dict[str, set[str]] = {}
        tender_documents_by_signature: dict[str, set[int]] = {}
        for row in tender_rows:
            tender_id = normalize_tender_id(row["raw_value"])
            signature = _v2_tender_signature(tender_id)
            if tender_id and signature:
                tender_ids_by_signature.setdefault(signature, set()).add(tender_id)
                tender_documents_by_signature.setdefault(signature, set()).add(row["document_id"])

        if not tender_ids_by_signature:
            return {
                "status": "no_explicit_batch_tender",
                "upload_batch_id": batch_id,
                "document_ids": completed_ids,
            }
        support_counts = {
            signature: len(document_ids)
            for signature, document_ids in tender_documents_by_signature.items()
        }
        # A set containing several printed tender IDs is a useful review cue,
        # but never grounds a cross-tender merge. Remove only obsolete legacy
        # batch associations before rebuilding evidence components.
        if len(tender_ids_by_signature) > 1:
            candidates = sorted({item for values in tender_ids_by_signature.values() for item in values})
            conn.execute(
                f"DELETE FROM v2_tender_associations WHERE document_id IN ({placeholders}) "
                "AND method IN ('upload_batch_explicit_tender', 'upload_batch_generated_tender')",
                completed_ids,
            )
            for current_id in completed_ids:
                _v2_insert_review(
                    conn,
                    current_id,
                    "conflicting_upload_batch_tender",
                    {
                        "upload_batch_id": batch_id,
                        "tender_ids": candidates,
                        "supporting_document_counts": support_counts,
                    },
                )
            return {
                "status": "conflicting_explicit_tenders",
                "upload_batch_id": batch_id,
                "tender_ids": candidates,
                "supporting_document_counts": support_counts,
                "reference_rebuild_document_ids": completed_ids,
            }

        return {
            "status": "evidence_component_grouping",
            "upload_batch_id": batch_id,
            "document_ids": completed_ids,
            "tender_ids": sorted(next(iter(tender_ids_by_signature.values()))),
        }


def seal_v2_upload_batch(upload_batch_id: str) -> dict:
    """Mark a browser upload selection complete and reconcile it when ready."""
    with get_connection() as conn:
        conn.execute("INSERT OR IGNORE INTO v2_upload_batches (batch_id) VALUES (?)", (upload_batch_id,))
        conn.execute(
            "UPDATE v2_upload_batches SET sealed_at = COALESCE(sealed_at, CURRENT_TIMESTAMP) WHERE batch_id = ?",
            (upload_batch_id,),
        )
        document = conn.execute(
            "SELECT id FROM v2_documents WHERE upload_batch_id = ? ORDER BY id DESC LIMIT 1",
            (upload_batch_id,),
        ).fetchone()
    if document is None:
        return {"status": "empty_upload_batch", "upload_batch_id": upload_batch_id}
    result = reconcile_v2_upload_batch(document["id"])
    if result.get("obsolete_generated_tender_ids"):
        delete_empty_v2_generated_tenders(result["obsolete_generated_tender_ids"])
    return result


def apply_v2_verified_grouping(
    tender_id: str,
    document_ids: list[int],
    *,
    title: str,
    id_origin: str = "generated",
) -> dict:
    """Apply a user-confirmed source grouping to completed V2 documents.

    This is deliberately separate from reference reconciliation: it records a
    supplied, verified tender boundary, replaces only the selected documents'
    V2 associations, and resolves their outstanding V2 grouping reviews. It
    never changes the source document, page, or extracted-reference records.
    """
    normalized_id = normalize_tender_id(tender_id)
    unique_ids = sorted(set(document_ids))
    if not normalized_id:
        raise ValueError("tender_id is required")
    if not unique_ids:
        raise ValueError("document_ids is required")
    if id_origin not in {"generated", "extracted"}:
        raise ValueError("id_origin must be 'generated' or 'extracted'")

    placeholders = ", ".join("?" for _ in unique_ids)
    with get_connection() as conn:
        rows = conn.execute(
            f"SELECT id, processing_state FROM v2_documents WHERE id IN ({placeholders})",
            unique_ids,
        ).fetchall()
        found = {row["id"]: row["processing_state"] for row in rows}
        missing = [document_id for document_id in unique_ids if document_id not in found]
        incomplete = [document_id for document_id in unique_ids if found.get(document_id) != "completed"]
        if missing:
            raise LookupError(f"V2 document(s) not found: {missing}")
        if incomplete:
            raise ValueError(f"V2 document(s) are not completed: {incomplete}")

        conn.execute(
            "INSERT OR IGNORE INTO tenders (tender_id, title, id_origin) VALUES (?, ?, ?)",
            (normalized_id, title, id_origin),
        )
        conn.execute("UPDATE tenders SET title = ? WHERE tender_id = ?", (title, normalized_id))
        conn.execute(
            f"DELETE FROM v2_tender_associations WHERE document_id IN ({placeholders})",
            unique_ids,
        )
        conn.executemany(
            "INSERT INTO v2_tender_associations (document_id, tender_id, method, status) "
            "VALUES (?, ?, 'verified_folder', 'confirmed')",
            [(document_id, normalized_id) for document_id in unique_ids],
        )
        conn.execute(
            f"UPDATE v2_documents SET grouping_state = 'manually_assigned' WHERE id IN ({placeholders})",
            unique_ids,
        )
        conn.execute(
            f"UPDATE v2_review_cases SET status = 'resolved', resolved_at = CURRENT_TIMESTAMP "
            f"WHERE document_id IN ({placeholders}) AND status = 'pending' "
            "AND review_type IN ('no_tender_id', 'conflicting_reference', 'multiple_tender_ids')",
            unique_ids,
        )
    return {"tender_id": normalized_id, "document_ids": unique_ids, "method": "verified_folder"}


def delete_empty_v2_generated_tenders(tender_ids: list[str]) -> list[str]:
    """Delete generated tender shells only when no records still depend on them."""
    deleted: list[str] = []
    for raw_tender_id in sorted(set(tender_ids)):
        tender_id = normalize_tender_id(raw_tender_id)
        if not tender_id:
            continue
        with get_connection() as conn:
            row = conn.execute(
                "SELECT id_origin FROM tenders WHERE tender_id = ?", (tender_id,)
            ).fetchone()
            if row is None or row["id_origin"] != "generated":
                continue
            dependencies = conn.execute(
                """SELECT
                     (SELECT COUNT(*) FROM documents WHERE tender_id = ?) +
                     (SELECT COUNT(*) FROM email_analysis WHERE tender_id = ?) +
                     (SELECT COUNT(*) FROM bids WHERE tender_id = ?) +
                     (SELECT COUNT(*) FROM tender_references WHERE tender_id = ?) +
                     (SELECT COUNT(*) FROM link_reviews WHERE tender_id = ? OR other_tender_id = ? OR resolved_tender_id = ?) +
                     (SELECT COUNT(*) FROM v2_tender_associations WHERE tender_id = ?) +
                     (SELECT COUNT(*) FROM v2_email_threads WHERE tender_id = ?) AS total""",
                (tender_id, tender_id, tender_id, tender_id, tender_id, tender_id, tender_id, tender_id, tender_id),
            ).fetchone()["total"]
            if dependencies:
                continue
            conn.execute("DELETE FROM tenders WHERE tender_id = ?", (tender_id,))
            deleted.append(tender_id)
    return deleted


def rebuild_legacy_v2_batch_associations() -> dict:
    """Replace pre-component automatic batch links while preserving human decisions."""
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT DISTINCT d.id, a.tender_id FROM v2_documents d
               JOIN v2_tender_associations a ON a.document_id = d.id
               WHERE d.grouping_state = 'auto_grouped'
                 AND a.method IN ('upload_batch_explicit_tender', 'upload_batch_generated_tender')"""
        ).fetchall()
        document_ids = sorted({row["id"] for row in rows})
        prior_generated = sorted({row["tender_id"] for row in rows if row["tender_id"].startswith("TND-")})
        if document_ids:
            placeholders = ", ".join("?" for _ in document_ids)
            conn.execute(
                f"DELETE FROM v2_tender_associations WHERE document_id IN ({placeholders}) "
                "AND method IN ('upload_batch_explicit_tender', 'upload_batch_generated_tender')",
                document_ids,
            )
            conn.execute(
                f"UPDATE v2_documents SET grouping_state = 'needs_review' WHERE id IN ({placeholders})",
                document_ids,
            )
    rebuilt = rebuild_v2_reference_grouping(document_ids)
    removed_tenders = delete_empty_v2_generated_tenders(prior_generated)
    return {"document_ids": document_ids, "rebuild": rebuilt, "removed_tenders": removed_tenders}


def delete_v2_documents(document_ids: list[int]) -> dict:
    """Delete a selected local V2 document set and any now-empty tender shells.

    Source fixtures are never touched. SQLite cascades remove only the selected
    documents' jobs, pages, extracted records, references, associations, and
    V2 reviews. A tender is removed only when no legacy or V2 record remains.
    """
    unique_ids = sorted(set(document_ids))
    if not unique_ids:
        raise ValueError("document_ids is required")
    placeholders = ", ".join("?" for _ in unique_ids)
    with get_connection() as conn:
        documents = conn.execute(
            f"SELECT id FROM v2_documents WHERE id IN ({placeholders})", unique_ids
        ).fetchall()
        found_ids = {row["id"] for row in documents}
        missing_ids = [document_id for document_id in unique_ids if document_id not in found_ids]
        if missing_ids:
            raise LookupError(f"V2 document(s) not found: {missing_ids}")
        tender_rows = conn.execute(
            f"SELECT DISTINCT tender_id FROM v2_tender_associations WHERE document_id IN ({placeholders})",
            unique_ids,
        ).fetchall()
        candidate_tenders = [row["tender_id"] for row in tender_rows]
        conn.execute(f"DELETE FROM v2_documents WHERE id IN ({placeholders})", unique_ids)

        removed_tenders: list[str] = []
        for tender_id in candidate_tenders:
            dependencies = conn.execute(
                """SELECT
                     (SELECT COUNT(*) FROM documents WHERE tender_id = ?) +
                     (SELECT COUNT(*) FROM email_analysis WHERE tender_id = ?) +
                     (SELECT COUNT(*) FROM bids WHERE tender_id = ?) +
                     (SELECT COUNT(*) FROM tender_references WHERE tender_id = ?) +
                     (SELECT COUNT(*) FROM link_reviews WHERE tender_id = ? OR other_tender_id = ? OR resolved_tender_id = ?) +
                     (SELECT COUNT(*) FROM v2_tender_associations WHERE tender_id = ?) +
                     (SELECT COUNT(*) FROM v2_email_threads WHERE tender_id = ?) +
                     (SELECT COUNT(*) FROM v2_email_messages WHERE tender_id = ?) AS total""",
                (tender_id, tender_id, tender_id, tender_id, tender_id, tender_id, tender_id, tender_id, tender_id, tender_id),
            ).fetchone()["total"]
            if dependencies == 0:
                conn.execute("DELETE FROM tenders WHERE tender_id = ?", (tender_id,))
                removed_tenders.append(tender_id)
    return {"deleted_document_ids": unique_ids, "removed_tenders": sorted(removed_tenders)}


def _v2_attach_by_reference(conn, document_id: int, tender_id: str, *, method: str = "typed_reference") -> None:
    """Attach one uniquely supported document/tender pair idempotently."""
    conn.execute(
        "INSERT OR IGNORE INTO v2_tender_associations (document_id, tender_id, method, status) VALUES (?, ?, ?, 'confirmed')",
        (document_id, tender_id, method),
    )


def _v2_backfill_reference_neighbors(conn, document_id: int, tender_id: str) -> None:
    """Link earlier unassigned V2 documents when their complete candidate set is unique."""
    current_references = conn.execute(
        "SELECT reference_type, normalized_value FROM v2_references WHERE document_id = ?", (document_id,)
    ).fetchall()
    neighbors: set[int] = set()
    for reference in current_references:
        rows = conn.execute(
            "SELECT DISTINCT document_id FROM v2_references WHERE reference_type = ? AND normalized_value = ? AND document_id != ?",
            (reference["reference_type"], reference["normalized_value"], document_id),
        ).fetchall()
        neighbors.update(row["document_id"] for row in rows)
    for neighbor_id in neighbors:
        grouping_state = conn.execute(
            "SELECT grouping_state FROM v2_documents WHERE id = ?", (neighbor_id,)
        ).fetchone()["grouping_state"]
        if grouping_state in {"kept_ungrouped", "manually_assigned"}:
            continue
        if conn.execute("SELECT 1 FROM v2_tender_associations WHERE document_id = ?", (neighbor_id,)).fetchone():
            continue
        references = conn.execute("SELECT reference_type, normalized_value FROM v2_references WHERE document_id = ?", (neighbor_id,)).fetchall()
        candidates = _v2_reference_candidates(conn, [(row["reference_type"], row["normalized_value"]) for row in references], exclude_document_id=neighbor_id)
        if candidates == {tender_id}:
            _v2_attach_by_reference(conn, neighbor_id, tender_id)
            conn.execute("UPDATE v2_documents SET grouping_state = 'auto_grouped' WHERE id = ?", (neighbor_id,))
            conn.execute("DELETE FROM v2_review_cases WHERE document_id = ? AND review_type = 'no_tender_id' AND status = 'pending'", (neighbor_id,))


def complete_v2_job(job_id: int, result: dict) -> None:
    """Atomically persist validated V2 output and create review-first associations."""
    with get_connection() as conn:
        job = conn.execute(
            """SELECT j.document_id, d.grouping_state
               FROM v2_processing_jobs j JOIN v2_documents d ON d.id = j.document_id
               WHERE j.id = ?""",
            (job_id,),
        ).fetchone()
        if job is None:
            raise LookupError(f"V2 job {job_id} not found")
        document_id = job["document_id"]
        grouping_locked = job["grouping_state"] in {"kept_ungrouped", "manually_assigned"}
        classification = result["classification"]
        conn.execute("DELETE FROM v2_document_pages WHERE document_id = ?", (document_id,))
        conn.execute("DELETE FROM v2_extraction_windows WHERE document_id = ?", (document_id,))
        conn.execute("DELETE FROM v2_references WHERE document_id = ?", (document_id,))
        conn.execute("DELETE FROM v2_derived_records WHERE document_id = ?", (document_id,))
        if grouping_locked:
            conn.execute(
                """DELETE FROM v2_review_cases WHERE document_id = ?
                   AND NOT (status = 'rejected' AND review_type IN
                       ('no_tender_id', 'conflicting_reference', 'multiple_tender_ids', 'conflicting_upload_batch_tender'))""",
                (document_id,),
            )
        else:
            conn.execute("DELETE FROM v2_review_cases WHERE document_id = ?", (document_id,))
            conn.execute("DELETE FROM v2_tender_associations WHERE document_id = ?", (document_id,))
        conn.execute(
            "UPDATE v2_documents SET page_count = ?, document_type = ?, processing_state = 'completed', analysis_json = ?, completed_at = CURRENT_TIMESTAMP WHERE id = ?",
            (result["page_count"], classification["document_type"], json.dumps(result), document_id),
        )
        conn.executemany(
            "INSERT INTO v2_document_pages (document_id, page_number, image_sha256, width_px, height_px, mime_type) VALUES (?, ?, ?, ?, ?, ?)",
            [(document_id, p["page_number"], p["image_sha256"], p["width_px"], p["height_px"], p["mime_type"]) for p in result["rendered_pages"]],
        )
        source_references = _v2_result_references(result)
        tender_ids = [
            normalize_tender_id(item["raw_value"])
            for item in source_references
            if item["reference_type"] == "tender_id"
        ]
        tender_ids = sorted({item for item in tender_ids if item})
        for reference in source_references:
            conn.execute(
                "INSERT OR IGNORE INTO v2_references (document_id, reference_type, raw_value, normalized_value, confidence, evidence_json) VALUES (?, ?, ?, ?, ?, ?)",
                (document_id, reference["reference_type"], reference["raw_value"], _normalize_v2_reference(reference["reference_type"], reference["raw_value"]), _reference_confidence(reference), json.dumps(reference["evidence"])),
            )
        reference_pairs = [
            (reference["reference_type"], _normalize_v2_reference(reference["reference_type"], reference["raw_value"]))
            for reference in source_references
        ]
        reference_candidates = _v2_reference_candidates(conn, reference_pairs, exclude_document_id=document_id)
        associated_tender: str | None = None
        if grouping_locked:
            pass
        elif len(tender_ids) == 1:
            tender_id = tender_ids[0]
            conn.execute("INSERT OR IGNORE INTO tenders (tender_id, id_origin) VALUES (?, 'extracted')", (tender_id,))
            conn.execute("INSERT INTO v2_tender_associations (document_id, tender_id, method, status) VALUES (?, ?, 'explicit', 'confirmed')", (document_id, tender_id))
            associated_tender = tender_id
            conflicting = sorted(reference_candidates - {tender_id})
            if conflicting:
                _v2_insert_review(conn, document_id, "conflicting_reference", {"explicit_tender_id": tender_id, "reference_candidates": conflicting})
        elif len(tender_ids) > 1:
            _v2_insert_review(conn, document_id, "multiple_tender_ids", {"tender_ids": tender_ids})
        elif len(reference_candidates) == 1:
            associated_tender = next(iter(reference_candidates))
            _v2_attach_by_reference(conn, document_id, associated_tender)
        elif len(reference_candidates) > 1:
            _v2_insert_review(conn, document_id, "conflicting_reference", {"reference_candidates": sorted(reference_candidates)})
        else:
            _v2_insert_review(conn, document_id, "no_tender_id", {"classification": classification["document_type"]})
        if associated_tender:
            conn.execute("UPDATE v2_documents SET grouping_state = 'auto_grouped' WHERE id = ?", (document_id,))
            _v2_backfill_reference_neighbors(conn, document_id, associated_tender)
        for window in result.get("detail_windows", []):
            window_id = conn.execute(
                "INSERT INTO v2_extraction_windows (document_id, page_numbers_json, detail_json, negotiation_events_json) VALUES (?, ?, ?, ?)",
                (document_id, json.dumps(window["page_numbers"]), json.dumps(window["detail"]), json.dumps(window.get("negotiation_events")) if window.get("negotiation_events") else None),
            ).lastrowid
            detail = window["detail"]
            detail_type = classification["document_type"]
            records = []
            if detail_type in ("purchase_order", "sales_contract"):
                records = [("po_line" if detail_type == "purchase_order" else "contract_line", row) for row in detail.get("lines", [])]
            elif detail_type == "shipping_document":
                records = [("shipping_component", row) for row in detail.get("components", [])]
            for kind, payload in records:
                conn.execute("INSERT INTO v2_derived_records (document_id, record_kind, source_window_id, payload_json) VALUES (?, ?, ?, ?)", (document_id, kind, window_id, json.dumps(payload)))
            for event in (window.get("negotiation_events") or {}).get("events", []):
                conn.execute("INSERT INTO v2_derived_records (document_id, record_kind, source_window_id, payload_json) VALUES (?, 'negotiation_event', ?, ?)", (document_id, window_id, json.dumps(event)))
                for line in event.get("commercial_lines", []):
                    conn.execute("INSERT INTO v2_derived_records (document_id, record_kind, source_window_id, payload_json) VALUES (?, 'offer_line', ?, ?)", (document_id, window_id, json.dumps(line)))
        for reason in classification.get("review_reasons", []):
            _v2_insert_review(conn, document_id, reason, {"classification": classification["document_type"]})
        conn.execute(
            "UPDATE v2_processing_jobs SET status = 'completed', stage = 'completed', current_page = ?, total_pages = ?, completed_at = CURRENT_TIMESTAMP WHERE id = ?",
            (result["page_count"], result["page_count"], job_id),
        )
    rebuild_v2_reference_grouping([document_id])
    batch_result = reconcile_v2_upload_batch(document_id)
    if batch_result.get("reference_rebuild_document_ids"):
        rebuild_v2_reference_grouping(batch_result["reference_rebuild_document_ids"])
    if batch_result.get("obsolete_generated_tender_ids"):
        delete_empty_v2_generated_tenders(batch_result["obsolete_generated_tender_ids"])


def fail_v2_job(job_id: int, error_text: str) -> None:
    """Persist a bounded failure message without exposing document content."""
    with get_connection() as conn:
        row = conn.execute("SELECT document_id FROM v2_processing_jobs WHERE id = ?", (job_id,)).fetchone()
        if row:
            conn.execute("UPDATE v2_processing_jobs SET status = 'failed', stage = 'failed', error_text = ?, completed_at = CURRENT_TIMESTAMP WHERE id = ?", (error_text[:1000], job_id))
            conn.execute("UPDATE v2_documents SET processing_state = 'failed', error_text = ? WHERE id = ?", (error_text[:1000], row["document_id"]))


def get_v2_job(job_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM v2_processing_jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def retry_v2_job(job_id: int) -> dict:
    """Requeue a failed V2 job without duplicating its source document."""
    with get_connection() as conn:
        changed = conn.execute("UPDATE v2_processing_jobs SET status = 'queued', stage = 'queued', current_page = NULL, total_pages = NULL, error_text = NULL, started_at = NULL, completed_at = NULL WHERE id = ? AND status = 'failed'", (job_id,)).rowcount
        if not changed:
            raise ValueError("only failed V2 jobs can be retried")
        row = conn.execute("SELECT document_id FROM v2_processing_jobs WHERE id = ?", (job_id,)).fetchone()
        conn.execute("UPDATE v2_documents SET processing_state = 'queued', error_text = NULL WHERE id = ?", (row["document_id"],))
        job = conn.execute("SELECT * FROM v2_processing_jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(job)


def sync_v2_email_messages(emails: list[dict]) -> None:
    """Upsert fetched Gmail messages into the V2 thread/message source model."""
    with get_connection() as conn:
        for email in emails:
            email_id = email.get("id") or email.get("email_id")
            thread_id = email.get("thread_id")
            if not email_id or not thread_id:
                continue
            tender_id = normalize_tender_id(email.get("tender_id"))
            if tender_id:
                conn.execute("INSERT OR IGNORE INTO tenders (tender_id, id_origin) VALUES (?, 'extracted')", (tender_id,))
            conn.execute("INSERT OR IGNORE INTO v2_email_threads (thread_id, tender_id) VALUES (?, ?)", (thread_id, tender_id))
            conn.execute("INSERT INTO v2_email_messages (email_id, thread_id, sender, recipient, subject, body, category, email_role, tender_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(email_id) DO UPDATE SET thread_id=excluded.thread_id, sender=excluded.sender, recipient=excluded.recipient, subject=excluded.subject, body=excluded.body, category=excluded.category, email_role=excluded.email_role, tender_id=excluded.tender_id, observed_at=CURRENT_TIMESTAMP", (email_id, thread_id, email.get("sender"), email.get("recipient"), email.get("subject"), email.get("body"), email.get("category"), email.get("email_role"), tender_id))


def list_v2_reviews(status: str | None = "pending") -> list[dict]:
    """Return V2 review cases with enough document context for a human decision."""
    sql = """SELECT r.*, d.file_name, d.document_type, d.page_count
             FROM v2_review_cases r
             LEFT JOIN v2_documents d ON d.id = r.document_id"""
    params: tuple = ()
    if status:
        sql += " WHERE r.status = ?"
        params = (status,)
    with get_connection() as conn:
        rows = conn.execute(sql + " ORDER BY r.id DESC", params).fetchall()
        document_ids = sorted({row["document_id"] for row in rows if row["document_id"] is not None})
        references_by_document: dict[int, list[dict]] = {document_id: [] for document_id in document_ids}
        candidates_by_document: dict[int, list[str]] = {document_id: [] for document_id in document_ids}
        linked_documents_by_document: dict[int, list[dict]] = {document_id: [] for document_id in document_ids}
        if document_ids:
            placeholders = ", ".join("?" for _ in document_ids)
            references = conn.execute(
                f"""SELECT document_id, reference_type, raw_value, normalized_value, confidence, evidence_json
                    FROM v2_references
                    WHERE document_id IN ({placeholders})
                    ORDER BY reference_type, raw_value""",
                document_ids,
            ).fetchall()
            for reference in references:
                references_by_document[reference["document_id"]].append({
                    "reference_type": reference["reference_type"],
                    "raw_value": reference["raw_value"],
                    "normalized_value": reference["normalized_value"],
                    "confidence": reference["confidence"],
                    "evidence": json.loads(reference["evidence_json"]),
                })
            for document_id, references_for_document in references_by_document.items():
                pairs = [(item["reference_type"], item["normalized_value"]) for item in references_for_document]
                candidates_by_document[document_id] = sorted(
                    _v2_reference_candidates(conn, pairs, exclude_document_id=document_id)
                )
                linked: dict[int, dict] = {}
                for reference_type, normalized_value in pairs:
                    matches = conn.execute(
                        """SELECT DISTINCT d.id, d.file_name FROM v2_references r
                           JOIN v2_documents d ON d.id = r.document_id
                           WHERE r.reference_type = ? AND r.normalized_value = ?
                             AND r.document_id != ? AND d.processing_state = 'completed'""",
                        (reference_type, normalized_value, document_id),
                    ).fetchall()
                    for match in matches:
                        linked[match["id"]] = {"document_id": match["id"], "file_name": match["file_name"]}
                linked_documents_by_document[document_id] = list(linked.values())
    result = []
    for row in rows:
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json") or "{}")
        item["references"] = references_by_document.get(item["document_id"], [])
        item["tender_candidates"] = candidates_by_document.get(item["document_id"], [])
        item["linked_documents"] = linked_documents_by_document.get(item["document_id"], [])
        result.append(item)
    return result


def _v2_reference_component(conn, seed_document_id: int) -> list[int]:
    """Return the safe, typed-reference component containing one completed PDF."""
    component: set[int] = set()
    pending = [seed_document_id]
    while pending:
        document_id = pending.pop()
        if document_id in component:
            continue
        row = conn.execute(
            "SELECT processing_state, grouping_state FROM v2_documents WHERE id = ?", (document_id,)
        ).fetchone()
        if row is None or row["processing_state"] != "completed":
            continue
        if row["grouping_state"] in {"kept_ungrouped", "manually_assigned"} and document_id != seed_document_id:
            continue
        component.add(document_id)
        references = conn.execute(
            "SELECT reference_type, normalized_value FROM v2_references WHERE document_id = ?",
            (document_id,),
        ).fetchall()
        for reference in references:
            matches = conn.execute(
                """SELECT d.id FROM v2_references r
                   JOIN v2_documents d ON d.id = r.document_id
                   WHERE r.reference_type = ? AND r.normalized_value = ?
                     AND d.processing_state = 'completed'""",
                (reference["reference_type"], reference["normalized_value"]),
            ).fetchall()
            pending.extend(match["id"] for match in matches if match["id"] not in component)
    return sorted(component)


def assign_v2_review_cluster(review_id: int, tender_id: str) -> dict:
    """Manually assign a review's full typed-reference component to one tender.

    The server derives the component from persisted typed references. Browser
    input therefore cannot silently expand a confirmation to arbitrary PDFs.
    Documents deliberately kept ungrouped or manually assigned elsewhere stay
    outside the component.
    """
    normalized_tender_id = normalize_tender_id(tender_id)
    if not normalized_tender_id:
        raise ValueError("tender_id is required")
    with get_connection() as conn:
        review = conn.execute("SELECT * FROM v2_review_cases WHERE id = ?", (review_id,)).fetchone()
        if review is None:
            raise LookupError(f"V2 review {review_id} not found")
        if review["status"] != "pending":
            raise ValueError(f"V2 review {review_id} is already {review['status']}")
        if review["document_id"] is None:
            raise ValueError("this review is not attached to a document")
        tender = conn.execute("SELECT 1 FROM tenders WHERE tender_id = ?", (normalized_tender_id,)).fetchone()
        if tender is None:
            raise ValueError(f"tender {normalized_tender_id} not found")
        document_ids = _v2_reference_component(conn, review["document_id"])
        if len(document_ids) < 2:
            raise ValueError("this document has no assignable typed-reference cluster")
        placeholders = ", ".join("?" for _ in document_ids)
        conn.execute(f"DELETE FROM v2_tender_associations WHERE document_id IN ({placeholders})", document_ids)
        conn.executemany(
            "INSERT INTO v2_tender_associations (document_id, tender_id, method, status) VALUES (?, ?, 'manual_cluster', 'confirmed')",
            [(document_id, normalized_tender_id) for document_id in document_ids],
        )
        conn.execute(
            f"UPDATE v2_documents SET grouping_state = 'manually_assigned' WHERE id IN ({placeholders})",
            document_ids,
        )
        conn.execute(
            f"""UPDATE v2_review_cases SET status = 'resolved', resolved_at = CURRENT_TIMESTAMP
                WHERE document_id IN ({placeholders}) AND status = 'pending'
                  AND review_type IN ('no_tender_id', 'conflicting_reference', 'multiple_tender_ids', 'conflicting_upload_batch_tender')""",
            document_ids,
        )
    return {"tender_id": normalized_tender_id, "document_ids": document_ids, "method": "manual_cluster"}


def resolve_v2_review(review_id: int, *, action: str, tender_id: str | None = None) -> dict:
    """Resolve one V2 review, optionally moving its PDF into an existing tender.

    Confirmation is a human override: it replaces every current V2 tender
    association for that PDF with one manual association. Other pending grouping
    reviews for the same PDF are resolved together, because they describe the
    same assignment decision. A missing tender or document fails without a
    partial update.
    """
    if action not in ("confirm", "reject"):
        raise ValueError("action must be 'confirm' or 'reject'")
    with get_connection() as conn:
        review = conn.execute("SELECT * FROM v2_review_cases WHERE id = ?", (review_id,)).fetchone()
        if review is None:
            raise LookupError(f"V2 review {review_id} not found")
        if review["status"] != "pending":
            raise ValueError(f"V2 review {review_id} is already {review['status']}")

        chosen_tender_id: str | None = None
        if action == "confirm":
            if review["document_id"] is None:
                raise ValueError("this review is not attached to a document")
            chosen_tender_id = normalize_tender_id(tender_id)
            if not chosen_tender_id:
                raise ValueError("tender_id is required to merge a document")
            tender = conn.execute("SELECT 1 FROM tenders WHERE tender_id = ?", (chosen_tender_id,)).fetchone()
            if tender is None:
                raise ValueError(f"tender {chosen_tender_id} not found")
            conn.execute("DELETE FROM v2_tender_associations WHERE document_id = ?", (review["document_id"],))
            conn.execute(
                "INSERT INTO v2_tender_associations (document_id, tender_id, method, status) VALUES (?, ?, 'manual_review', 'confirmed')",
                (review["document_id"], chosen_tender_id),
            )
            conn.execute(
                "UPDATE v2_documents SET grouping_state = 'manually_assigned' WHERE id = ?",
                (review["document_id"],),
            )
            conn.execute(
                """UPDATE v2_review_cases SET status = 'resolved', resolved_at = CURRENT_TIMESTAMP
                   WHERE document_id = ? AND status = 'pending'
                     AND review_type IN ('no_tender_id', 'conflicting_reference', 'multiple_tender_ids', 'conflicting_upload_batch_tender')""",
                (review["document_id"],),
            )
        else:
            if review["document_id"] is not None:
                # "Keep ungrouped" is an explicit human choice. Remove a
                # generated/reference association and dismiss every grouping
                # review for this PDF together, so it appears only in the
                # ungrouped tray until it is deliberately reopened.
                conn.execute("DELETE FROM v2_tender_associations WHERE document_id = ?", (review["document_id"],))
                conn.execute(
                    "UPDATE v2_documents SET grouping_state = 'kept_ungrouped' WHERE id = ?",
                    (review["document_id"],),
                )
                conn.execute(
                    """UPDATE v2_review_cases SET status = 'rejected', resolved_at = CURRENT_TIMESTAMP
                       WHERE document_id = ? AND status = 'pending'
                         AND review_type IN ('no_tender_id', 'conflicting_reference', 'multiple_tender_ids', 'conflicting_upload_batch_tender')""",
                    (review["document_id"],),
                )
            else:
                conn.execute(
                    "UPDATE v2_review_cases SET status = 'rejected', resolved_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (review_id,),
                )

        updated = conn.execute("SELECT * FROM v2_review_cases WHERE id = ?", (review_id,)).fetchone()
    item = dict(updated)
    item["payload"] = json.loads(item.pop("payload_json") or "{}")
    item["tender_id"] = chosen_tender_id
    return item


def reopen_v2_review(review_id: int) -> dict:
    """Return one deliberately ungrouped PDF to the pending document-review queue."""
    with get_connection() as conn:
        review = conn.execute("SELECT * FROM v2_review_cases WHERE id = ?", (review_id,)).fetchone()
        if review is None:
            raise LookupError(f"V2 review {review_id} not found")
        if review["status"] != "rejected":
            raise ValueError(f"V2 review {review_id} is not kept ungrouped")
        if review["document_id"] is None:
            raise ValueError("this review is not attached to a document")
        changed = conn.execute(
            """UPDATE v2_review_cases SET status = 'pending', resolved_at = NULL
               WHERE document_id = ? AND status = 'rejected'
                 AND review_type IN ('no_tender_id', 'conflicting_reference', 'multiple_tender_ids', 'conflicting_upload_batch_tender')""",
            (review["document_id"],),
        ).rowcount
        if not changed:
            raise ValueError("this document has no rejected grouping review to reopen")
        conn.execute(
            "UPDATE v2_documents SET grouping_state = 'needs_review' WHERE id = ?",
            (review["document_id"],),
        )
        updated = conn.execute("SELECT * FROM v2_review_cases WHERE id = ?", (review_id,)).fetchone()
    item = dict(updated)
    item["payload"] = json.loads(item.pop("payload_json") or "{}")
    return item


def get_v2_document(document_id: int) -> dict | None:
    with get_connection() as conn:
        document = conn.execute("SELECT id, file_name, sha256, page_count, document_type, processing_state, grouping_state, analysis_json, error_text, created_at, completed_at FROM v2_documents WHERE id = ?", (document_id,)).fetchone()
        if document is None:
            return None
        pages = conn.execute("SELECT page_number, image_sha256, width_px, height_px, mime_type FROM v2_document_pages WHERE document_id = ? ORDER BY page_number", (document_id,)).fetchall()
        associations = conn.execute("SELECT tender_id, method, status FROM v2_tender_associations WHERE document_id = ?", (document_id,)).fetchall()
        reviews = conn.execute("SELECT id, review_type, payload_json, status FROM v2_review_cases WHERE document_id = ?", (document_id,)).fetchall()
        references = conn.execute("SELECT reference_type, raw_value, normalized_value, confidence, evidence_json FROM v2_references WHERE document_id = ? ORDER BY reference_type, raw_value", (document_id,)).fetchall()
        windows = conn.execute("SELECT page_numbers_json, detail_json, negotiation_events_json FROM v2_extraction_windows WHERE document_id = ? ORDER BY id", (document_id,)).fetchall()
    item = dict(document)
    item["analysis"] = json.loads(item.pop("analysis_json") or "null")
    item["pages"] = [dict(row) for row in pages]
    item["tender_associations"] = [dict(row) for row in associations]
    item["reviews"] = [{**dict(row), "payload": json.loads(row["payload_json"])} for row in reviews]
    item["references"] = [{**dict(row), "evidence": json.loads(row["evidence_json"])} for row in references]
    item["extraction_windows"] = [
        {"page_numbers": json.loads(row["page_numbers_json"]), "detail": json.loads(row["detail_json"]), "negotiation_events": json.loads(row["negotiation_events_json"]) if row["negotiation_events_json"] else None}
        for row in windows
    ]
    return item


def get_v2_document_pdf(document_id: int) -> tuple[str, bytes] | None:
    """Return the stored source PDF for an existing V2 document, if available."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT file_name, source_pdf FROM v2_documents WHERE id = ?", (document_id,)
        ).fetchone()
    if row is None:
        return None
    return row["file_name"], row["source_pdf"]


def list_v2_documents() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT d.id, d.file_name, d.page_count, d.document_type, d.processing_state, d.grouping_state,
                      d.error_text, d.created_at, d.completed_at,
                      MAX(j.id) AS job_id,
                      GROUP_CONCAT(a.tender_id) AS tender_ids
               FROM v2_documents d
               LEFT JOIN v2_processing_jobs j ON j.document_id = d.id
               LEFT JOIN v2_tender_associations a ON a.document_id = d.id AND a.status = 'confirmed'
               GROUP BY d.id ORDER BY d.id DESC"""
        ).fetchall()
    return [dict(row) for row in rows]


def save_email_analysis(
    email_id: str,
    *,
    thread_id=None, sender=None, subject=None,
    category=None, email_role=None,
    tender_id=None, tender_id_source=None,
    confidence=None, status="ok", attachment_context_hash=None,
):
    tender_id = normalize_tender_id(tender_id)
    with get_connection() as conn:
        if conn.execute(
            "SELECT 1 FROM deleted_emails WHERE email_id = ?", (email_id,)
        ).fetchone():
            tender_id = None
            tender_id_source = None
        if tender_id:
            conn.execute(
                "INSERT OR IGNORE INTO tenders (tender_id) VALUES (?)",
                (tender_id,),
            )
        conn.execute(
            """
            INSERT INTO email_analysis
                (email_id, thread_id, sender, subject, category, email_role,
                 tender_id, tender_id_source, confidence, status, attachment_context_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(email_id) DO UPDATE SET
                thread_id = excluded.thread_id,
                sender = excluded.sender,
                subject = excluded.subject,
                category = excluded.category,
                email_role = excluded.email_role,
                tender_id = excluded.tender_id,
                tender_id_source = excluded.tender_id_source,
                confidence = excluded.confidence,
                status = excluded.status,
                attachment_context_hash = COALESCE(
                    excluded.attachment_context_hash, email_analysis.attachment_context_hash
                ),
                analyzed_at = CURRENT_TIMESTAMP
            """,
            (email_id, thread_id, sender, subject, category, email_role,
             tender_id, tender_id_source, confidence, status, attachment_context_hash),
        )


def get_email_analysis(email_id: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM email_analysis WHERE email_id = ?", (email_id,)
        ).fetchone()
    return dict(row) if row else None


def is_email_deleted(email_id: str) -> bool:
    """Whether an inbox message was suppressed after its tender was deleted."""
    with get_connection() as conn:
        return conn.execute(
            "SELECT 1 FROM deleted_emails WHERE email_id = ?", (email_id,)
        ).fetchone() is not None


def prepare_deleted_thread_restore(thread_id: str, email_ids: list[str], anchor_email_id: str) -> dict | None:
    """Clear deletion suppression and stale analysis for one explicitly restored Gmail thread."""
    if anchor_email_id not in email_ids:
        return None
    with get_connection() as conn:
        marker = conn.execute(
            "SELECT thread_id, tender_id FROM deleted_emails WHERE email_id = ?",
            (anchor_email_id,),
        ).fetchone()
        if marker is None or (marker["thread_id"] and marker["thread_id"] != thread_id):
            return None
        ids = list(dict.fromkeys(email_ids))
        marks = ",".join("?" for _ in ids)
        conn.execute(f"DELETE FROM deleted_emails WHERE email_id IN ({marks})", ids)
        conn.execute(f"DELETE FROM bids WHERE email_id IN ({marks})", ids)
        conn.execute(
            f"DELETE FROM tender_references WHERE source_kind = 'email' AND source_id IN ({marks})",
            ids,
        )
        conn.execute(
            f"DELETE FROM email_analysis WHERE email_id IN ({marks}) OR thread_id = ?",
            (*ids, thread_id),
        )
    return {"tender_id": marker["tender_id"]}


def get_emails_for_tender(tender_id: str, unchecked_attachments_only: bool = False) -> list[dict]:
    """Linked email analyses, optionally only those whose PDF attachments need scanning."""
    sql = ("SELECT * FROM email_analysis e WHERE e.tender_id = ? AND e.status = 'ok' "
           "AND NOT EXISTS (SELECT 1 FROM deleted_emails d WHERE d.email_id = e.email_id)")
    if unchecked_attachments_only:
        sql += " AND e.attachments_checked_at IS NULL"
    sql += " ORDER BY e.analyzed_at"
    with get_connection() as conn:
        rows = conn.execute(sql, (normalize_tender_id(tender_id),)).fetchall()
    return [dict(r) for r in rows]


def mark_email_attachments_checked(email_id: str) -> None:
    """Record a complete attachment scan; failed scans remain eligible for retry."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE email_analysis SET attachments_checked_at = CURRENT_TIMESTAMP WHERE email_id = ?",
            (email_id,),
        )

def get_tender_id_for_thread(thread_id: str | None) -> str | None:
    if not thread_id:
        return None
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT tender_id FROM email_analysis
            WHERE thread_id = ? AND tender_id IS NOT NULL
            ORDER BY analyzed_at ASC
            LIMIT 1
            """,
            (thread_id,),
        ).fetchone()
    return row["tender_id"] if row else None

def insert_bid(
    *,
    tender_id: str,
    email_id: str | None = None,
    document_id: int | None = None,
    offer_seq: int | None = None,
    offered_at: str | None = None,
    vendor_name: str | None = None,
    vendor_email: str | None = None,
    body_text: str | None = None,
    attachment_names: str | None = None,
) -> bool:
    """Insert a pending bid. Returns True if a new row was created, False if
    this email_id (or this document_id + offer_seq) was already registered.
    Raises ValueError if neither email_id nor document_id is given (INSERT OR
    IGNORE would otherwise swallow the CHECK violation silently)."""
    if email_id is None and document_id is None:
        raise ValueError("insert_bid needs an email_id or a document_id")
    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO bids
                (tender_id, email_id, document_id, offer_seq, offered_at,
                 vendor_name, vendor_email, body_text, attachment_names)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (normalize_tender_id(tender_id), email_id, document_id, offer_seq,
             offered_at, vendor_name, vendor_email, body_text, attachment_names),
        )
        return cursor.rowcount == 1


def get_bids_for_tender(tender_id: str) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM bids WHERE tender_id = ? ORDER BY created_at",
            (normalize_tender_id(tender_id),),
        ).fetchall()
    return [dict(r) for r in rows]

def get_bids_to_extract(tender_id: str) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM bids
            WHERE tender_id = ? AND extraction_status IN ('pending', 'failed')
            ORDER BY created_at
            """,
            (normalize_tender_id(tender_id),),
        ).fetchall()
    return [dict(r) for r in rows]


def get_unregistered_vendor_bid_emails(tender_id: str) -> list[dict]:
    """Linked vendor emails whose cached analysis has no bid row yet; read only."""
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT e.email_id, e.thread_id, e.tender_id,
                      (SELECT b.tender_id FROM bids b WHERE b.email_id = e.email_id LIMIT 1)
                        AS existing_bid_tender_id
               FROM email_analysis e
               WHERE e.tender_id = ? AND e.status = 'ok' AND e.email_role = 'vendor_bid'
                 AND NOT EXISTS (
                     SELECT 1 FROM bids b
                     WHERE b.email_id = e.email_id AND b.tender_id = e.tender_id
                 )
                 AND NOT EXISTS (SELECT 1 FROM deleted_emails d WHERE d.email_id = e.email_id)
               ORDER BY e.analyzed_at, e.email_id""",
            (normalize_tender_id(tender_id),),
        ).fetchall()
    return [dict(row) for row in rows]


def save_email_offer_bids(email_id: str, offers: list[dict]) -> int:
    """Upsert one email's extracted offers by sequence and remove stale later rounds."""
    with get_connection() as conn:
        source = conn.execute(
            "SELECT * FROM bids WHERE email_id = ? ORDER BY offer_seq LIMIT 1", (email_id,)
        ).fetchone()
        if source is None:
            return 0
        source = dict(source)
        for offer in offers:
            seq = offer["offer_seq"]
            existing = conn.execute(
                "SELECT id FROM bids WHERE email_id = ? AND offer_seq = ?", (email_id, seq)
            ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE bids SET offered_at = ?, vendor_name = ?, body_text = ?,
                              extracted_json = ?, extraction_status = 'extracted'
                       WHERE id = ?""",
                    (offer["offered_at"], offer["vendor_name"], offer["body_text"],
                     offer["extracted_json"], existing["id"]),
                )
            else:
                conn.execute(
                    """INSERT INTO bids
                       (tender_id, email_id, offer_seq, offered_at, vendor_name, vendor_email,
                        body_text, attachment_names, extracted_json, extraction_status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'extracted')""",
                    (source["tender_id"], email_id, seq, offer["offered_at"],
                     offer["vendor_name"], source["vendor_email"], offer["body_text"],
                     source["attachment_names"], offer["extracted_json"]),
                )
        keep_through = max((offer["offer_seq"] for offer in offers), default=0)
        conn.execute(
            "DELETE FROM bids WHERE email_id = ? AND offer_seq > ?", (email_id, keep_through)
        )
        return len(offers)


def save_bid_extraction(
    bid_id: int,
    *,
    status: str,
    extracted_json: str | None = None,
    vendor_name: str | None = None,
):
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE bids
            SET extraction_status = ?,
                extracted_json    = ?,
                vendor_name       = COALESCE(?, vendor_name)
            WHERE id = ?
            """,
            (status, extracted_json, vendor_name, bid_id),
        )


def get_document_by_sha(sha256: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM documents WHERE sha256 = ?", (sha256,)
        ).fetchone()
    return dict(row) if row else None


def save_document(
    *,
    source: str,
    file_name: str,
    sha256: str,
    page_count: int,
    full_text: str,
    page_quality_json: str,
    extraction_status: str,
    email_id: str | None = None,
    gmail_attachment_id: str | None = None,
) -> tuple[int, bool]:
    """Insert a document. Returns (document_id, is_new); is_new is False
    when a document with the same sha256 already exists."""
    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO documents
                (source, file_name, sha256, email_id, gmail_attachment_id,
                 page_count, full_text, page_quality_json, extraction_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (source, file_name, sha256, email_id, gmail_attachment_id,
             page_count, full_text, page_quality_json, extraction_status),
        )
        if cursor.rowcount == 1:
            return cursor.lastrowid, True
        row = conn.execute(
            "SELECT id FROM documents WHERE sha256 = ?", (sha256,)
        ).fetchone()
    return row["id"], False

def get_documents_to_analyze(redo: bool = False) -> list[dict]:
    sql = "SELECT id, file_name, page_count, full_text FROM documents"
    if not redo:
        sql += " WHERE document_type IS NULL"
    with get_connection() as conn:
        rows = conn.execute(sql + " ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def save_document_analysis(
    document_id: int,
    *,
    document_type: str,
    contains_json: str,
    analysis_json: str,
    references: list[tuple[str, str]],
) -> None:
    """Store the analysis and replace this document's references, in one transaction."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE documents SET document_type = ?, contains_json = ?, analysis_json = ? WHERE id = ?",
            (document_type, contains_json, analysis_json, document_id),
        )
        conn.execute(
            "DELETE FROM tender_references WHERE source_kind = 'document' AND source_id = ?",
            (str(document_id),),
        )
        conn.executemany(
            "INSERT OR IGNORE INTO tender_references (ref_type, ref_value, source_kind, source_id) "
            "VALUES (?, ?, 'document', ?)",
            [(t, v, str(document_id)) for t, v in references],
        )

def get_documents_for_linking() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, file_name, document_type, tender_id FROM documents ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


def get_document_references() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT ref_type, ref_value, source_id FROM tender_references "
            "WHERE source_kind = 'document'"
        ).fetchall()
    return [dict(r) for r in rows]


def next_generated_tender_id() -> str:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT tender_id FROM tenders WHERE tender_id GLOB 'TND-[0-9]*'"
        ).fetchall()
    numbers = [int(r["tender_id"][4:]) for r in rows if r["tender_id"][4:].isdigit()]
    return f"TND-{max(numbers, default=0) + 1:04d}"


def assign_documents_to_tender(
    tender_id: str, doc_ids: list[int], link_method: str, id_origin: str = "extracted"
) -> None:
    """Create the tender if needed and attach the still-unassigned documents
    (and their reference rows) to it."""
    tender_id = normalize_tender_id(tender_id)
    with get_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO tenders (tender_id, id_origin) VALUES (?, ?)",
            (tender_id, id_origin),
        )
        for doc_id in doc_ids:
            conn.execute(
                "UPDATE documents SET tender_id = ?, link_method = ? "
                "WHERE id = ? AND tender_id IS NULL",
                (tender_id, link_method, doc_id),
            )
            conn.execute(
                "UPDATE tender_references SET tender_id = ? "
                "WHERE source_kind = 'document' AND source_id = ?",
                (tender_id, str(doc_id)),
            )


def add_link_review(
    *,
    review_type: str,
    source_kind: str,
    source_id: str,
    tender_id: str,
    other_tender_id: str | None,
    reason: str,
) -> bool:
    """Queue a link for human review. Returns False if the same review already exists
    (pending or resolved), so a decided case is not asked again."""
    with get_connection() as conn:
        exists = conn.execute(
            "SELECT 1 FROM link_reviews WHERE review_type = ? "
            "AND source_kind = ? AND source_id = ? AND tender_id = ? AND other_tender_id IS ?",
            (review_type, source_kind, source_id, tender_id, other_tender_id),
        ).fetchone()
        if exists:
            return False
        conn.execute(
            "INSERT INTO link_reviews "
            "(review_type, source_kind, source_id, tender_id, other_tender_id, reason) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (review_type, source_kind, source_id, tender_id, other_tender_id, reason),
        )
        return True

def delete_generated_tenders() -> int:
    """Generated tenders (TND-...) are derived data: unlink their documents and delete
    them, with any reviews about them, so linking can be re-run from scratch."""
    with get_connection() as conn:
        ids = [r["tender_id"] for r in conn.execute(
            "SELECT tender_id FROM tenders WHERE id_origin = 'generated'")]
        for tid in ids:
            conn.execute("UPDATE documents SET tender_id = NULL, link_method = NULL WHERE tender_id = ?", (tid,))
            conn.execute("UPDATE tender_references SET tender_id = NULL WHERE tender_id = ?", (tid,))
            conn.execute(
                "DELETE FROM link_reviews WHERE tender_id = ? OR other_tender_id = ? OR resolved_tender_id = ?",
                (tid, tid, tid),
            )
            conn.execute("DELETE FROM tenders WHERE tender_id = ?", (tid,))
    return len(ids)

def get_offer_documents(
    redo: bool = False, document_types: tuple[str, ...] = ("email_thread", "quotation"),
) -> list[dict]:
    """Linked offer-source PDFs without saved bids or a completed no-offer result."""
    marks = ", ".join("?" for _ in document_types)
    sql = (
        "SELECT d.id, d.file_name, d.document_type, d.tender_id, d.full_text FROM documents d "
        f"WHERE d.document_type IN ({marks})"
    )
    if not redo:
        sql += (" AND d.offer_extraction_status != 'no_offers' "
                "AND NOT EXISTS (SELECT 1 FROM bids b WHERE b.document_id = d.id)")
    with get_connection() as conn:
        rows = conn.execute(sql + " ORDER BY d.id", document_types).fetchall()
    return [dict(r) for r in rows]


def get_thread_documents(redo: bool = False) -> list[dict]:
    """Email-thread subset used when restoring a deleted Gmail conversation."""
    return get_offer_documents(redo, ("email_thread",))


def replace_document_offers(document_id: int, offers: list[dict]) -> int:
    """Atomically replace one document's extracted offers after a complete extraction."""
    with get_connection() as conn:
        conn.execute("DELETE FROM bids WHERE document_id = ?", (document_id,))
        conn.executemany(
            """INSERT INTO bids
                (tender_id, document_id, offer_seq, message_seq, offered_at, vendor_name,
                 body_text, extracted_json, extraction_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'extracted')""",
            [(
                normalize_tender_id(offer["tender_id"]), document_id, offer["offer_seq"],
                offer["message_seq"], offer["offered_at"], offer["vendor_name"],
                offer["body_text"], offer["extracted_json"],
            ) for offer in offers],
        )
        conn.execute(
            "UPDATE documents SET offer_extraction_status = 'extracted' WHERE id = ?",
            (document_id,),
        )
        return len(offers)


def set_thread_offer_extraction_status(document_id: int, status: str) -> None:
    """Record a failed or completed empty thread extraction without changing bids."""
    if status not in ("failed", "no_offers"):
        raise ValueError(f"Unsupported offer extraction status: {status}")
    with get_connection() as conn:
        conn.execute(
            "UPDATE documents SET offer_extraction_status = ? WHERE id = ?",
            (status, document_id),
        )


def get_offer_bids(tender_id: str | None = None) -> list[dict]:
    """Extracted offers from Gmail emails and thread documents."""
    sql = (
        "SELECT id, tender_id, email_id, document_id, offer_seq, offered_at, vendor_name, "
        "vendor_email, extracted_json, accepted, duplicate_of "
        "FROM bids WHERE extraction_status = 'extracted' "
        "AND (document_id IS NOT NULL OR email_id IS NOT NULL)"
    )
    params: tuple = ()
    if tender_id:
        sql += " AND tender_id = ?"
        params = (normalize_tender_id(tender_id),)
    with get_connection() as conn:
        rows = conn.execute(
            sql + " ORDER BY tender_id, offered_at, created_at, document_id, email_id, offer_seq",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def get_tender_ids_with_offers() -> list[str]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT tender_id FROM bids WHERE extraction_status = 'extracted' "
            "AND (document_id IS NOT NULL OR email_id IS NOT NULL) ORDER BY tender_id"
        ).fetchall()
    return [r["tender_id"] for r in rows]


def get_contract_texts(tender_id: str) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, file_name, full_text FROM documents "
            "WHERE tender_id = ? AND document_type = 'sales_contract' ORDER BY id",
            (normalize_tender_id(tender_id),),
        ).fetchall()
    return [dict(r) for r in rows]


def save_offer_marks(tender_id: str, duplicates: list[tuple[int, int]], accepted_ids: list[int]) -> None:
    """Replace the duplicate/accepted marks of one tender's extracted offers.
    duplicates = [(duplicate_bid_id, original_bid_id), ...]."""
    tid = normalize_tender_id(tender_id)
    with get_connection() as conn:
        conn.execute(
            "UPDATE bids SET accepted = 0, duplicate_of = NULL "
            "WHERE tender_id = ? AND extraction_status = 'extracted' "
            "AND (document_id IS NOT NULL OR email_id IS NOT NULL)", (tid,))
        conn.executemany(
            "UPDATE bids SET duplicate_of = ? WHERE id = ?",
            [(orig, dup) for dup, orig in duplicates])
        conn.executemany("UPDATE bids SET accepted = 1 WHERE id = ?", [(i,) for i in accepted_ids])


def _tender_deletion_counts(conn, tender_id: str) -> dict | None:
    """Count all local rows that would be removed with one tender."""
    exists = conn.execute("SELECT 1 FROM tenders WHERE tender_id = ?", (tender_id,)).fetchone()
    if not exists:
        return None
    counts = {
        "tenders": 1,
        "emails": conn.execute(
            "SELECT COUNT(*) FROM email_analysis WHERE tender_id = ?", (tender_id,)
        ).fetchone()[0],
        "documents": conn.execute(
            "SELECT COUNT(*) FROM documents WHERE tender_id = ?", (tender_id,)
        ).fetchone()[0],
        "offers": conn.execute(
            """SELECT COUNT(*) FROM bids b WHERE b.tender_id = ?
               OR b.email_id IN (SELECT email_id FROM email_analysis WHERE tender_id = ?)
               OR b.document_id IN (SELECT id FROM documents WHERE tender_id = ?)""",
            (tender_id, tender_id, tender_id),
        ).fetchone()[0],
        "references": conn.execute(
            """SELECT COUNT(*) FROM tender_references r WHERE r.tender_id = ?
               OR (r.source_kind = 'email' AND r.source_id IN
                   (SELECT email_id FROM email_analysis WHERE tender_id = ?))
               OR (r.source_kind = 'document' AND r.source_id IN
                   (SELECT CAST(id AS TEXT) FROM documents WHERE tender_id = ?))""",
            (tender_id, tender_id, tender_id),
        ).fetchone()[0],
        "reviews": conn.execute(
            """SELECT COUNT(*) FROM link_reviews
               WHERE tender_id = ? OR other_tender_id = ? OR resolved_tender_id = ?""",
            (tender_id, tender_id, tender_id),
        ).fetchone()[0],
    }
    return counts


def get_tender_deletion_preview(tender_id: str) -> dict | None:
    """Return counts of all local database rows associated with a tender."""
    tid = normalize_tender_id(tender_id)
    with get_connection() as conn:
        counts = _tender_deletion_counts(conn, tid)
    return {"tender_id": tid, "records": counts} if counts is not None else None


def delete_tender_data(tender_id: str) -> dict | None:
    """Atomically remove one tender and its associated local records."""
    tid = normalize_tender_id(tender_id)
    with get_connection() as conn:
        counts = _tender_deletion_counts(conn, tid)
        if counts is None:
            return None
        conn.execute(
            "INSERT OR IGNORE INTO deleted_emails (email_id, thread_id, tender_id) "
            "SELECT email_id, thread_id, tender_id FROM email_analysis WHERE tender_id = ?", (tid,)
        )
        conn.execute(
            """DELETE FROM bids WHERE tender_id = ?
               OR email_id IN (SELECT email_id FROM email_analysis WHERE tender_id = ?)
               OR document_id IN (SELECT id FROM documents WHERE tender_id = ?)""",
            (tid, tid, tid),
        )
        conn.execute(
            """DELETE FROM tender_references WHERE tender_id = ?
               OR (source_kind = 'email' AND source_id IN
                   (SELECT email_id FROM email_analysis WHERE tender_id = ?))
               OR (source_kind = 'document' AND source_id IN
                   (SELECT CAST(id AS TEXT) FROM documents WHERE tender_id = ?))""",
            (tid, tid, tid),
        )
        conn.execute(
            """DELETE FROM link_reviews
               WHERE tender_id = ? OR other_tender_id = ? OR resolved_tender_id = ?""",
            (tid, tid, tid),
        )
        conn.execute("DELETE FROM documents WHERE tender_id = ?", (tid,))
        conn.execute("DELETE FROM email_analysis WHERE tender_id = ?", (tid,))
        conn.execute("DELETE FROM tenders WHERE tender_id = ?", (tid,))
    return {"tender_id": tid, "deleted": counts}

def list_tenders() -> list[dict]:
    """One summary row per tender, for a tender list screen."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT t.tender_id, t.title, t.id_origin, t.created_at,
              (SELECT COUNT(*) FROM documents d WHERE d.tender_id = t.tender_id) +
              (SELECT COUNT(*) FROM v2_tender_associations a
                 WHERE a.tender_id = t.tender_id AND a.status = 'confirmed') AS document_count,
              (SELECT COUNT(*) FROM email_analysis e WHERE e.tender_id = t.tender_id) AS email_count,
              (SELECT COUNT(*) FROM bids b
                 WHERE b.tender_id = t.tender_id AND b.duplicate_of IS NULL) AS bid_count,
              (SELECT COUNT(*) FROM bids b
                 WHERE b.tender_id = t.tender_id AND b.accepted = 1) AS accepted_count
            FROM tenders t
            ORDER BY t.created_at, t.tender_id
            """
        ).fetchall()
    return [dict(r) for r in rows]


def get_tender_view(tender_id: str) -> dict | None:
    """Everything stored for one tender: its emails, its documents grouped by
    type, and its bids (duplicates and the accepted offer marked). Returns None
    if the tender does not exist. Bid bodies are left out (they can be large)."""
    tid = normalize_tender_id(tender_id)
    with get_connection() as conn:
        tender = conn.execute("SELECT * FROM tenders WHERE tender_id = ?", (tid,)).fetchone()
        if tender is None:
            return None
        emails = conn.execute(
            "SELECT email_id, thread_id, sender, subject, category, email_role, status, attachments_checked_at, "
            "tender_id_source, analyzed_at FROM email_analysis "
            "WHERE tender_id = ? ORDER BY analyzed_at", (tid,)
        ).fetchall()
        docs = conn.execute(
            "SELECT id, file_name, document_type, contains_json, page_count, "
            "extraction_status, offer_extraction_status, link_method "
            "FROM documents WHERE tender_id = ? ORDER BY id",
            (tid,),
        ).fetchall()
        v2_docs = conn.execute(
            """SELECT d.id, d.file_name, d.document_type, d.page_count,
                      d.processing_state, d.completed_at, a.method AS link_method
               FROM v2_documents d
               JOIN v2_tender_associations a ON a.document_id = d.id
               WHERE a.tender_id = ? AND a.status = 'confirmed'
               ORDER BY d.id""",
            (tid,),
        ).fetchall()
        bids = conn.execute(
            "SELECT id, email_id, document_id, offer_seq, message_seq, offered_at, vendor_name, "
            "vendor_email, extraction_status, extracted_json, accepted, duplicate_of "
            "FROM bids WHERE tender_id = ? "
            "ORDER BY document_id IS NULL, document_id, offer_seq, created_at", (tid,)
        ).fetchall()

    documents: dict[str, list[dict]] = {}
    for d in docs:
        item = dict(d)
        item["contains"] = json.loads(item.pop("contains_json") or "[]")
        documents.setdefault(item["document_type"] or "unclassified", []).append(item)

    bid_list = []
    for b in bids:
        item = dict(b)
        item["offer"] = json.loads(item.pop("extracted_json") or "null")
        item["accepted"] = bool(item["accepted"])
        bid_list.append(item)

    return {
        "tender": dict(tender),
        "emails": [dict(e) for e in emails],
        "documents": documents,
        "v2_documents": [dict(d) for d in v2_docs],
        "bids": bid_list,
    }

def list_link_reviews(status: str | None = "pending") -> list[dict]:
    """Link reviews (pending by default; status=None for all), with the source file name."""
    sql = (
        "SELECT r.*, d.file_name AS source_file_name, d.tender_id AS source_current_tender "
        "FROM link_reviews r LEFT JOIN documents d "
        "ON r.source_kind = 'document' AND d.id = CAST(r.source_id AS INTEGER)"
    )
    params: tuple = ()
    if status:
        sql += " WHERE r.status = ?"
        params = (status,)
    with get_connection() as conn:
        rows = conn.execute(sql + " ORDER BY r.id", params).fetchall()
    return [dict(r) for r in rows]


def resolve_link_review(review_id: int, *, action: str, tender_id: str | None = None) -> dict:
    """Human decision on one review. action = 'confirm' (tender_id must be one of the
    review's tenders; the source moves there) or 'reject' (nothing changes).
    Raises LookupError if the review does not exist, ValueError for invalid input."""
    if action not in ("confirm", "reject"):
        raise ValueError("action must be 'confirm' or 'reject'")
    with get_connection() as conn:
        r = conn.execute("SELECT * FROM link_reviews WHERE id = ?", (review_id,)).fetchone()
        if r is None:
            raise LookupError(f"Review {review_id} not found")
        if r["status"] != "pending":
            raise ValueError(f"Review {review_id} is already {r['status']}")

        chosen = None
        if action == "confirm":
            chosen = normalize_tender_id(tender_id)
            allowed = {t for t in (r["tender_id"], r["other_tender_id"]) if t}
            if chosen not in allowed:
                raise ValueError(f"tender_id must be one of {sorted(allowed)}")
            if r["source_kind"] == "document":
                doc_id = int(r["source_id"])
                conn.execute(
                    "UPDATE documents SET tender_id = ?, link_method = 'manual' WHERE id = ?",
                    (chosen, doc_id))
                conn.execute(
                    "UPDATE tender_references SET tender_id = ? "
                    "WHERE source_kind = 'document' AND source_id = ?", (chosen, str(doc_id)))
                conn.execute("UPDATE bids SET tender_id = ? WHERE document_id = ?", (chosen, doc_id))
            else:
                conn.execute("UPDATE email_analysis SET tender_id = ? WHERE email_id = ?",
                             (chosen, r["source_id"]))
                conn.execute("UPDATE bids SET tender_id = ? WHERE email_id = ?",
                             (chosen, r["source_id"]))

        conn.execute(
            "UPDATE link_reviews SET status = ?, resolved_tender_id = ?, "
            "resolved_at = CURRENT_TIMESTAMP WHERE id = ?",
            ("confirmed" if action == "confirm" else "rejected", chosen, review_id))
        row = conn.execute("SELECT * FROM link_reviews WHERE id = ?", (review_id,)).fetchone()
    return dict(row)

init_db()


if __name__ == "__main__":
    save_email_analysis(
        "e1", thread_id="t1", subject="RFQ: Caustic soda",
        category="quotation_request", email_role="tender_issue",
        tender_id="tdr-2025-0142", tender_id_source="regex", confidence=0.95,
    )
    save_email_analysis(
        "e2", thread_id="t1", subject="Re: RFQ: Caustic soda",
        category="quotation_request", email_role="vendor_bid",
        tender_id=" TDR-2025-0142 ", tender_id_source="thread", confidence=0.8,
    )

    print(get_email_analysis("e1"))
    print(len(get_emails_for_tender("TDR-2025-0142")))
    print(get_email_analysis("nope"))

    try:
        with get_connection() as conn:
            conn.execute("INSERT INTO bids (tender_id, email_id) VALUES ('NOPE', 'x')")
    except sqlite3.IntegrityError as e:
        print("Foreign key enforced:", e)
