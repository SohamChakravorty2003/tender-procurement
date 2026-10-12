"""Read-only fixture guardrails for the named V2 tender-grouping demonstrations.

Run with ``uv run python -m app.v2_regression_checks``.  This does not invoke
Bedrock or write SQLite; it makes missing/changed source sets visible before a
targeted VLM regression run is approved.
"""

import argparse
import hashlib
import sqlite3
from pathlib import Path


EXPECTED_PDF_COUNTS = {
    "MM25042 - SARVESH (DOMSE)": 5,
    "MM25034 - CALDERYS (DOMSE)": 5,
    "EXPSE 65 FZCOSE - SHEFFIELD REF (EXPSE)": 10,
    "E 280224 1 - SANAC (EXPSE)": 11,
    "E 200525 2 - BILASPUR (DOMSE)": 5,
    "E 060825 1 - MAITHAN (DOMSE)": 15,
    "E 050625 2 - TYK (EXPSE)": 11,
    "E 050225 1 - KILICLAR (EXPSE)": 15,
}


def check_named_fixtures(root: Path) -> dict[str, int]:
    """Assert each named read-only tender set has its expected PDF baseline."""
    counts: dict[str, int] = {}
    for folder_name, expected_count in EXPECTED_PDF_COUNTS.items():
        matches = [path for path in root.rglob(folder_name) if path.is_dir()]
        if len(matches) != 1:
            raise AssertionError(f"expected one fixture folder for {folder_name!r}, found {len(matches)}")
        count = len(list(matches[0].glob("*.pdf")))
        if count != expected_count:
            raise AssertionError(f"{folder_name}: expected {expected_count} PDFs, found {count}")
        counts[folder_name] = count
    return counts


def check_uploaded_fixture_results(root: Path, database: Path) -> dict[str, int]:
    """Validate completed, non-manual V2 results against the named source sets.

    This intentionally does not invoke Bedrock or alter SQLite. It is run after
    a targeted fixture upload and rejects missing classification, references
    without page evidence, manual grouping, and tender IDs shared by two named
    fixture sets. Unassociated PDFs must remain visible as grouping reviews.
    """
    check_named_fixtures(root)
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    seen_tenders: dict[str, str] = {}
    results: dict[str, int] = {}
    try:
        for folder_name, expected_count in EXPECTED_PDF_COUNTS.items():
            folder = next(path for path in root.rglob(folder_name) if path.is_dir())
            pdfs = sorted(folder.glob("*.pdf"))
            document_ids: list[int] = []
            for pdf in pdfs:
                sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()
                row = connection.execute(
                    "SELECT id, document_type, processing_state FROM v2_documents WHERE sha256 = ?", (sha256,)
                ).fetchone()
                if row is None:
                    raise AssertionError(f"{folder_name}: {pdf.name} was not uploaded")
                if row["processing_state"] != "completed" or not row["document_type"]:
                    raise AssertionError(f"{folder_name}: {pdf.name} is not completely classified")
                document_ids.append(row["id"])
                references = connection.execute(
                    "SELECT evidence_json FROM v2_references WHERE document_id = ?", (row["id"],)
                ).fetchall()
                if any(not reference["evidence_json"] or reference["evidence_json"] == "[]" for reference in references):
                    raise AssertionError(f"{folder_name}: {pdf.name} has a reference without page evidence")
                manual = connection.execute(
                    """SELECT 1 FROM v2_tender_associations
                       WHERE document_id = ? AND method IN ('manual_review', 'manual_cluster', 'verified_folder')""",
                    (row["id"],),
                ).fetchone()
                if manual:
                    raise AssertionError(f"{folder_name}: {pdf.name} has a forbidden manual grouping override")
                grouped = connection.execute(
                    "SELECT 1 FROM v2_tender_associations WHERE document_id = ? AND status = 'confirmed'",
                    (row["id"],),
                ).fetchone()
                pending_review = connection.execute(
                    """SELECT 1 FROM v2_review_cases WHERE document_id = ? AND status = 'pending'
                       AND review_type IN ('no_tender_id', 'conflicting_reference', 'multiple_tender_ids')""",
                    (row["id"],),
                ).fetchone()
                if not grouped and not pending_review:
                    raise AssertionError(f"{folder_name}: {pdf.name} is neither grouped nor queued for review")

            placeholders = ", ".join("?" for _ in document_ids)
            associations = connection.execute(
                f"""SELECT DISTINCT tender_id FROM v2_tender_associations
                    WHERE document_id IN ({placeholders}) AND status = 'confirmed'""",
                document_ids,
            ).fetchall()
            tender_ids = {row["tender_id"] for row in associations}
            if len(tender_ids) > 1:
                raise AssertionError(f"{folder_name}: expected at most one automatic tender, found {sorted(tender_ids)}")
            if tender_ids:
                tender_id = next(iter(tender_ids))
                prior = seen_tenders.setdefault(tender_id, folder_name)
                if prior != folder_name:
                    raise AssertionError(f"forbidden cross-tender merge: {prior} and {folder_name} share {tender_id}")
            results[folder_name] = len(document_ids)
    finally:
        connection.close()
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run read-only V2 fixture regression checks")
    parser.add_argument("--uploaded", action="store_true", help="also validate completed V2 database results")
    parser.add_argument("--database", type=Path, default=Path(__file__).resolve().parent / "procurement.db")
    args = parser.parse_args()
    fixture_root = Path(__file__).resolve().parents[1] / "MINCHEM-DATA-RAW"
    result = check_named_fixtures(fixture_root)
    if args.uploaded:
        result = check_uploaded_fixture_results(fixture_root, args.database)
    print(result)
