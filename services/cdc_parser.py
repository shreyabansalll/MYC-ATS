from __future__ import annotations

import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

import pymupdf

from pipeline.extractor import _ocr_pdf_page
from pipeline.maritime import detect_rank


DATE_PATTERNS = (
    r"\d{1,2}[/-]\d{1,2}[/-]\d{4}",
    r"\d{1,2}[/-]\d{4}",
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{4}",
)
DATE_TOKEN = "|".join(DATE_PATTERNS)
SERVICE_RANGE = re.compile(
    rf"(?P<start>{DATE_TOKEN})\s*(?:-|–|—|to)\s*(?P<end>{DATE_TOKEN}|present|current)",
    re.I,
)
MONTH_NAMES = (
    "jan", "feb", "mar", "apr", "may", "jun",
    "jul", "aug", "sep", "oct", "nov", "dec",
)


def _parse_date(value: str) -> tuple[str | None, bool]:
    value = (value or "").strip()
    if value.casefold() in {"present", "current"}:
        return date.today().isoformat(), False
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%m/%Y", "%m-%Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat(), False
        except ValueError:
            pass
    match = re.fullmatch(r"([A-Za-z]+)\s+(\d{4})", value)
    if match:
        short = match.group(1).casefold()[:3]
        if short in MONTH_NAMES:
            return date(int(match.group(2)), MONTH_NAMES.index(short) + 1, 1).isoformat(), True
    return None, True


def _source_evidence(raw_text: str, start: int, end: int, limit: int = 300) -> str:
    left = max(0, start - 100)
    right = min(len(raw_text), end + 100)
    return raw_text[left:right][:limit].replace("\n", " ").strip()


def _text_for_page(path: str, page_index: int, page) -> dict:
    text = (page.get_text("text") or "").strip()
    method = "text"
    if len(text) < 30:
        ocr = _ocr_pdf_page(path, page_index)
        if ocr.get("text"):
            text = ocr["text"].strip()
            method = "ocr"
        else:
            method = "ocr_attempted" if ocr.get("status") != "disabled" else "ocr_disabled"
    return {"text": text, "method": method}


def parse_cdc_pdf(
    path: str,
    *,
    authorized_sample: bool = False,
    manual_verification_status: str = "not_verified",
) -> dict:
    """Parse a text-native or scanned CDC without upgrading resume claims."""
    pdf_path = Path(path)
    if not pdf_path.exists():
        return {"status": "error", "error": "CDC file not found", "sailings": [], "pages": []}

    pages = []
    raw_pages = []
    with pymupdf.open(str(pdf_path)) as document:
        for index in range(document.page_count):
            page = document.load_page(index)
            extracted = _text_for_page(str(pdf_path), index, page)
            raw_pages.append(extracted["text"])
            pages.append({
                "page_number": index + 1,
                "raw_text": extracted["text"],
                "extraction_method": extracted["method"],
                "confidence": 0.95 if extracted["method"] == "text" else 0.7 if extracted["method"] == "ocr" else 0.0,
                "manual_verification_status": manual_verification_status,
            })

    combined = "\n".join(raw_pages)
    clean = re.sub(r"[ \t]+", " ", combined)
    sailings = []
    lines = clean.splitlines()
    for page_index, page_text in enumerate(raw_pages):
        local_lines = page_text.splitlines()
        for line_index, line in enumerate(local_lines):
            match = SERVICE_RANGE.search(line)
            if not match:
                continue
            context = "\n".join(local_lines[max(0, line_index - 1):min(len(local_lines), line_index + 2)])
            start, ambiguous_start = _parse_date(match.group("start"))
            end, ambiguous_end = _parse_date(match.group("end"))
            rank = detect_rank(context)
            if rank == "unknown":
                rank = None
            vessel_match = re.search(
                r"\b(?:M\s*/\s*[TV]|MV|MT|Vessel(?:\s+name)?\s*[:=-])\s*([A-Z][A-Za-z0-9 .()'-]{1,48})",
                context,
                re.I,
            )
            sailings.append({
                "rank": rank,
                "vessel": vessel_match.group(1).strip(" .,-") if vessel_match else None,
                "sign_on": start,
                "sign_off": end,
                "date_precision": "day_or_month",
                "date_ambiguous": ambiguous_start or ambiguous_end,
                "page_number": page_index + 1,
                "row_or_region": line.strip(),
                "raw_text": context.strip(),
                "normalized_value": line.strip(),
                "extraction_method": pages[page_index]["extraction_method"],
                "confidence": pages[page_index]["confidence"],
                "manual_verification_status": manual_verification_status,
                "verification_status": "verified_authorized_cdc" if authorized_sample else "unverified_cdc",
            })

    cdc_number_match = re.search(r"\bCDC(?:\s*(?:No|Number))?\s*[:#]?\s*([A-Z0-9/-]{4,})", clean, re.I)
    indos_match = re.search(r"\bINDOS(?:\s*(?:No|Number))?\s*[:#]?\s*([A-Z0-9/-]{4,})", clean, re.I)
    name_match = re.search(r"(?:Candidate|Seafarer|Name)\s*[:=-]\s*(.+)", clean, re.I | re.M)
    if sailings:
        last_sailing = sorted(
            (s for s in sailings if s.get("sign_off")),
            key=lambda s: s["sign_off"],
            reverse=True,
        )[0]
    else:
        last_sailing = None

    return {
        "status": "parsed",
        "authorized_sample": authorized_sample,
        "candidate_name": name_match.group(1).strip()[:120] if name_match else None,
        "cdc_number": cdc_number_match.group(1) if cdc_number_match else None,
        "indos_number": indos_match.group(1) if indos_match else None,
        "issuing_authority": None,
        "issue_information": "not_present",
        "validity_information": "not_present",
        "sailings": sailings,
        "last_sailing": last_sailing,
        "pages": pages,
        "missing_fields": sorted({
            field for field, value in {
                "issuing_authority": None,
                "issue_information": "not_present",
                "validity_information": "not_present",
            }.items() if value in (None, "not_present")
        }),
        "provenance": {
            "filename": pdf_path.name,
            "source_type": "authorized_cdc" if authorized_sample else "uploaded_cdc",
            "manual_verification_status": manual_verification_status,
        },
        "ocr": {
            "enabled": os.getenv("TESSERACT_OCR_ENABLED", "1") not in {"0", "false", "no"},
            "pages_ocr": sum(p["extraction_method"] == "ocr" for p in pages),
        },
    }
