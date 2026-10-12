"""Durable, in-process worker entry point for transitional V2 document jobs."""

from __future__ import annotations

import logging

from app.tender_store import claim_v2_job, complete_v2_job, fail_v2_job, update_v2_job_progress
from app.v2_document_pipeline import analyze_pdf_document


logger = logging.getLogger(__name__)


def process_v2_document_job(job_id: int) -> None:
    """Claim, analyse, and persist a queued V2 document job; failures remain retryable."""
    claimed = claim_v2_job(job_id)
    if claimed is None:
        return
    job, pdf_bytes = claimed
    try:
        result = analyze_pdf_document(
            pdf_bytes,
            on_stage=lambda stage, current_page, total_pages: update_v2_job_progress(
                job_id, stage, current_page=current_page, total_pages=total_pages
            ),
        )
        update_v2_job_progress(job_id, "persisting", current_page=result.page_count, total_pages=result.page_count)
        update_v2_job_progress(job_id, "grouping", current_page=result.page_count, total_pages=result.page_count)
        complete_v2_job(job_id, result.model_dump(mode="json"))
    except Exception as exc:
        logger.exception("V2 document job failed job_id=%s", job_id)
        fail_v2_job(job_id, str(exc))
