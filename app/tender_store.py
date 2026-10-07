import json
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
        """)
        _migrate_existing_db(conn)


def normalize_tender_id(tender_id: str | None) -> str | None:
    if not tender_id or not tender_id.strip():
        return None
    return tender_id.strip().upper()


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
              (SELECT COUNT(*) FROM documents d WHERE d.tender_id = t.tender_id) AS document_count,
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
