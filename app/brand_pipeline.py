"""
brand_pipeline.py
-----------------
The end-to-end "process a brand guidelines document" pipeline.

Steps:
  1. Download the uploaded file from Supabase Storage (service role).
  2. Extract plain text (PDF via pypdf, DOCX via zipfile/document.xml,
     plain text fallback).
  3. Ask Anthropic Claude to turn it into structured brand JSON.
  4. Persist as a new active row in `brand_contexts` (versioned).

This module is pure logic: it does NOT know about FastAPI or HTTP.
`job_runner.py` and `app.py` call into it.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass
from typing import Any, Optional

from anthropic import Anthropic
from pypdf import PdfReader
from supabase import Client

from brand_context import replace_active

# ---------- system prompt ----------
SYSTEM_PROMPT = """You are a brand strategist. Convert the supplied brand
guidelines document into a single JSON object with this shape:

{
  "brand_name": string,
  "voice": { "tone": string[], "do": string[], "dont": string[] },
  "audience": { "primary": string, "secondary": string[] },
  "positioning": string,
  "value_props": string[],
  "messaging_pillars": [{ "name": string, "description": string }],
  "visual": {
    "colors": [{ "name": string, "hex": string, "usage": string }],
    "typography": [{ "name": string, "usage": string }],
    "imagery": string
  },
  "vocabulary": { "preferred": string[], "avoid": string[] },
  "examples": { "headlines": string[], "captions": string[] }
}

Return ONLY the JSON. Use [] or "" when info is missing. Never invent hex
codes — omit colors you cannot find."""


# ---------- input/output ----------
@dataclass
class PipelineInput:
    concept_id: str
    file_path: str        # storage path within bucket
    file_name: str
    mime_type: str


@dataclass
class PipelineResult:
    brand_context_id: str
    version: int
    chars_extracted: int


# ---------- text extraction ----------
def extract_text(raw: bytes, mime_type: str, file_name: str) -> str:
    name = (file_name or "").lower()
    if mime_type == "application/pdf" or name.endswith(".pdf"):
        return _extract_pdf(raw)
    if (
        mime_type
        == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        or name.endswith(".docx")
    ):
        return _extract_docx(raw)
    # plain text fallback
    try:
        return raw.decode("utf-8", errors="ignore")
    except Exception:
        return ""


def _extract_pdf(raw: bytes) -> str:
    reader = PdfReader(io.BytesIO(raw))
    parts: list[str] = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            continue
    return "\n".join(parts)


def _extract_docx(raw: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        with z.open("word/document.xml") as f:
            xml = f.read().decode("utf-8", errors="ignore")
    # Strip tags, keep paragraph breaks for readability
    xml = re.sub(r"</w:p>", "\n", xml)
    return re.sub(r"<[^>]+>", "", xml)


# ---------- AI structuring ----------
def structure_with_ai(
    anthropic: Anthropic,
    *,
    text: str,
    model: str,
    max_chars: int = 120_000,
) -> dict[str, Any]:
    """Call Claude and return parsed JSON. Raises on invalid JSON."""
    msg = anthropic.messages.create(
        model=model,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": text[:max_chars]}],
    )
    body = "".join(
        block.text for block in msg.content if getattr(block, "type", "") == "text"
    ).strip()

    # Tolerate ```json fences
    if body.startswith("```"):
        body = re.sub(r"^```(?:json)?\s*", "", body)
        body = re.sub(r"\s*```$", "", body)

    return json.loads(body)


# ---------- top-level pipeline ----------
def run_pipeline(
    *,
    sb: Client,
    anthropic: Anthropic,
    bucket: str,
    model: str,
    inp: PipelineInput,
) -> PipelineResult:
    raw = sb.storage.from_(bucket).download(inp.file_path)
    text = extract_text(raw, inp.mime_type, inp.file_name)
    if not text.strip():
        raise RuntimeError("No text extracted from document")

    structured = structure_with_ai(anthropic, text=text, model=model)

    row = replace_active(
        sb,
        concept_id=inp.concept_id,
        content=structured,
        source_file_path=inp.file_path,
    )
    return PipelineResult(
        brand_context_id=row["id"],
        version=row["version"],
        chars_extracted=len(text),
    )
