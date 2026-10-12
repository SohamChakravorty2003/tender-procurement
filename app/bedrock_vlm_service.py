"""Dedicated AWS Bedrock Converse client for image-based V2 extraction.

The service accepts already-rendered page images and returns strictly validated
Pydantic output.  It deliberately has no database dependency: persistence,
job scheduling, and prompts belong to subsequent V2 phases.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import TypeVar

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError

from app.document_render_service import RenderedPage


logger = logging.getLogger(__name__)

# Make the VLM component usable from a CLI, worker, or FastAPI alike.  This
# loads local configuration only; AWS credentials remain in Boto3's provider
# chain and are never read from or written to this project file.
load_dotenv()

MAX_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 2
TResult = TypeVar("TResult", bound=BaseModel)


class BedrockVlmError(RuntimeError):
    """Raised when a VLM request cannot yield valid structured output."""


@dataclass(frozen=True)
class BedrockVlmConfig:
    """Required non-secret Bedrock routing configuration."""

    region_name: str
    model_id: str


@dataclass(frozen=True)
class VlmResponse:
    """Validated VLM result plus non-sensitive request metadata."""

    result: BaseModel
    raw_text: str
    request_id: str | None
    model_id: str
    page_numbers: tuple[int, ...]


def get_bedrock_vlm_config() -> BedrockVlmConfig:
    """Read required model routing values without handling credentials.

    Credentials intentionally remain in Boto3's standard provider chain.  A
    missing setting fails before any request is attempted.
    """
    region_name = os.getenv("AWS_REGION", "").strip()
    model_id = os.getenv("BEDROCK_VLM_MODEL_ID", "").strip()
    missing = [name for name, value in (("AWS_REGION", region_name), ("BEDROCK_VLM_MODEL_ID", model_id)) if not value]
    if missing:
        raise BedrockVlmError(f"Missing Bedrock VLM configuration: {', '.join(missing)}")
    return BedrockVlmConfig(region_name=region_name, model_id=model_id)


def create_bedrock_runtime_client(config: BedrockVlmConfig | None = None):
    """Create a Bedrock Runtime client using AWS's default credential chain."""
    resolved = config or get_bedrock_vlm_config()
    return boto3.client("bedrock-runtime", region_name=resolved.region_name)


def _parse_json_object(raw_text: str) -> dict:
    """Parse one JSON object, tolerating only a surrounding Markdown fence."""
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if len(lines) < 3 or not lines[-1].strip().startswith("```"):
            raise BedrockVlmError("VLM returned an incomplete Markdown JSON fence")
        cleaned = "\n".join(lines[1:-1]).strip()
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].lstrip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise BedrockVlmError("VLM response was not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise BedrockVlmError("VLM response must be one JSON object")
    return parsed


def _response_text(response: dict) -> str:
    """Extract text content from a Bedrock Converse response or raise."""
    try:
        blocks = response["output"]["message"]["content"]
    except (KeyError, TypeError) as exc:
        raise BedrockVlmError("Bedrock response did not contain message content") from exc
    text = "\n".join(block["text"] for block in blocks if isinstance(block, dict) and isinstance(block.get("text"), str))
    if not text.strip():
        raise BedrockVlmError("Bedrock response did not contain text output")
    return text


def _message_content(prompt: str, pages: list[RenderedPage]) -> list[dict]:
    """Build ordered Converse content blocks from page JPEGs and an instruction."""
    if not prompt.strip():
        raise BedrockVlmError("VLM prompt must not be empty")
    if not pages:
        raise BedrockVlmError("VLM requests require at least one rendered page")
    content: list[dict] = [{"text": prompt}]
    for page in pages:
        if page.mime_type != "image/jpeg":
            raise BedrockVlmError(f"page {page.page_number} must be JPEG")
        content.append({"image": {"format": "jpeg", "source": {"bytes": page.image_bytes}}})
    return content


def invoke_vlm_json(
    *,
    prompt: str,
    pages: list[RenderedPage],
    result_model: type[TResult],
    client=None,
    config: BedrockVlmConfig | None = None,
    max_attempts: int = MAX_ATTEMPTS,
) -> VlmResponse:
    """Send ordered page images to Bedrock and validate exactly one JSON result.

    Only transient Bedrock client failures are retried. Invalid JSON and invalid
    Pydantic output fail closed so callers can persist a failed/review state.
    """
    if max_attempts < 1:
        raise BedrockVlmError("max_attempts must be at least one")
    resolved = config or get_bedrock_vlm_config()
    runtime_client = client or create_bedrock_runtime_client(resolved)
    page_numbers = tuple(page.page_number for page in pages)
    if page_numbers != tuple(sorted(set(page_numbers))):
        raise BedrockVlmError("VLM page numbers must be ascending and unique")

    request = {
        "modelId": resolved.model_id,
        "messages": [{"role": "user", "content": _message_content(prompt, pages)}],
        "inferenceConfig": {"temperature": 0},
    }
    for attempt in range(1, max_attempts + 1):
        try:
            response = runtime_client.converse(**request)
            raw_text = _response_text(response)
            result = result_model.model_validate(_parse_json_object(raw_text))
            metadata = response.get("ResponseMetadata") or {}
            logger.info(
                "Bedrock VLM request completed model=%s pages=%s request_id=%s",
                resolved.model_id,
                page_numbers,
                metadata.get("RequestId"),
            )
            return VlmResponse(
                result=result,
                raw_text=raw_text,
                request_id=metadata.get("RequestId"),
                model_id=resolved.model_id,
                page_numbers=page_numbers,
            )
        except (BedrockVlmError, ValidationError):
            raise
        except (ClientError, BotoCoreError) as exc:
            if attempt == max_attempts:
                raise BedrockVlmError(f"Bedrock request failed after {attempt} attempt(s): {exc}") from exc
            logger.warning("Bedrock VLM request failed attempt=%s pages=%s", attempt, page_numbers)
            time.sleep(RETRY_DELAY_SECONDS * attempt)
        except Exception as exc:
            raise BedrockVlmError(f"Unexpected Bedrock VLM failure: {exc}") from exc

    raise BedrockVlmError("Bedrock VLM request did not return a result")
