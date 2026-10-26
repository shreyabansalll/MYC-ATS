"""Two-layer scoring: Resume Matcher generic ATS + optional maritime overlay.

Layer 1 always runs. Layer 2 (certificates, sea service, rank, cruise-line
JD mapping) runs only when career_track / MYC rank fields say it should.
"""

from __future__ import annotations

import re

from config import REWRITE_IMPROVEMENT_RETRY_POINTS
from generic_ats.adapter import flatten_resume_text, parsed_to_resume
from generic_ats.career_track import maritime_overlay_active, resolve_target_jd
from generic_ats.keywords import extract_job_keywords
from generic_ats.layer import score_generic, score_generic_rewritten
from generic_ats.text_match import keyword_in_text
from pipeline.maritime import (
    detect_rank,
    detect_vessel_types,
    extract_certificates,
    maritime_score,
)


_METRIC_RE = re.compile(
    r"(?<!\w)(?:[$€£₹][ \t]*\d[\d,]*(?:\.\d+)?|"
    r"\d[\d,]*(?:\.\d+)?[ \t]*(?:%|percent|months?|years?|yrs?|days?|weeks?|"
    r"hours?|hrs?|minutes?|mins?|grt|dwt|vessels?|ships?|crew|tons?|tonnes?|"
    r"kg|km|miles?))(?!\w)",
    re.IGNORECASE,
)
_CERT_CODE_RE = re.compile(
    r"\b(?=[A-Z0-9/-]*[A-Z])(?=[A-Z0-9/-]*\d)[A-Z0-9]+(?:[-/][A-Z0-9]+)*\b",
    re.IGNORECASE,
)
_BARE_CERT_NUMBER_RE = re.compile(r"\b\d{5,}\b")


def build_protected_signals(extracted: dict, parsed: dict, layers: dict) -> list[dict[str, str]]:
    """Collect source-backed scoring signals before invoking the rewriter."""
    raw_text = extracted.get("raw_text") or parsed.get("raw_text") or ""
    sections = parsed.get("sections") or {}
    signals: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(text: str, kind: str, section: str, restore_text: str = "") -> None:
        text = text.strip()
        key = text.casefold()
        if not text or key in seen:
            return
        seen.add(key)
        signals.append({
            "text": text,
            "kind": kind,
            "section": section,
            "restore_text": restore_text or text,
        })

    role = layers.get("role") or ""
    job_keywords = extract_job_keywords(layers.get("job_description") or "", role=role)
    scoring_text = flatten_resume_text(parsed_to_resume(parsed, title=role)) or raw_text
    for keyword in job_keywords.get("keywords") or []:
        if not isinstance(keyword, str) or not keyword_in_text(keyword, scoring_text):
            continue
        section = next(
            (
                key
                for key in ("skills", "experience", "summary", "technical_expertise")
                if keyword_in_text(keyword, str(sections.get(key) or ""))
            ),
            "skills",
        )
        add(keyword, "keyword", section)

    cert_lines: list[tuple[str, str]] = []
    for section in ("certifications", "documents", "license"):
        for line in str(sections.get(section) or "").splitlines():
            line = line.strip()
            if line:
                cert_lines.append((line, section))
                add(line, "certificate", section)

    for certificate in extract_certificates(raw_text):
        match = re.search(re.escape(certificate), raw_text, re.IGNORECASE)
        if match:
            add(match.group(), "certificate", "certifications")

    for line, section in cert_lines:
        for pattern in (_CERT_CODE_RE, _BARE_CERT_NUMBER_RE):
            for match in pattern.finditer(line):
                add(match.group(), "certificate_code", section, line)

    for match in _METRIC_RE.finditer(raw_text):
        metric = match.group().strip()
        if not metric or not re.search(r"\d", metric):
            continue
        line_start = raw_text.rfind("\n", 0, match.start()) + 1
        line_end = raw_text.find("\n", match.end())
        source_line = raw_text[line_start:] if line_end == -1 else raw_text[line_start:line_end]
        source_line = source_line.strip()
        if re.search(r"[\w.+-]{1,64}@[\w-]+(?:\.[\w-]+)+", source_line):
            continue
        section = next(
            (
                key
                for key, value in sections.items()
                if isinstance(value, str) and metric.casefold() in value.casefold()
            ),
            "experience",
        )
        add(metric, "metric", section, source_line or metric)

    checks = layers.get("generic", {}).get("parse_checks") or []
    detected_sections = next(
        (
            check.get("params", {}).get("found", [])
            for check in checks
            if check.get("id") == "section_headings"
        ),
        [],
    )
    section_labels = {
        "summary": "Summary",
        "experience": "Experience",
        "education": "Education",
        "skills": "Skills",
    }
    for section in detected_sections:
        label = section_labels.get(section)
        if label:
            add(label, "section_heading", section)

    maritime = layers.get("maritime")
    if maritime:
        rank = maritime.get("rank_detected")
        if isinstance(rank, str) and rank and rank != "unknown":
            add(rank, "rank", "experience")
        for vessel_type in maritime.get("vessel_types") or []:
            add(str(vessel_type), "vessel_type", "experience")

    return signals


def score_layers(
    extracted: dict,
    parsed: dict,
    *,
    job_description: str = "",
    career_track: str = "",
    target_rank: str = "",
    target_cruise_line: str = "",
    pdf_path: str | None = None,
    ext: str = "pdf",
    resume: dict | None = None,
    source: str = "upload",
) -> dict:
    """Run Layer 1 always; Layer 2 only when the overlay is active.

    Returns generic + maritime results separately. maritime is None when
    the overlay is off so callers can omit it from the API payload.
    """
    detected_rank = (parsed.get("rank") or "").strip()
    if not detected_rank:
        detected_rank = detect_rank(extracted.get("raw_text") or parsed.get("raw_text") or "")
        if detected_rank:
            parsed = {**parsed, "rank": detected_rank}
    overlay = maritime_overlay_active(
        career_track=career_track,
        target_rank=target_rank,
        target_cruise_line=target_cruise_line,
        detected_rank=detected_rank,
    )
    role = (target_rank or detected_rank or "").strip()
    jd = resolve_target_jd(
        career_track,
        job_description,
        rank=role,
    )
    generic = score_generic(
        extracted,
        parsed,
        job_description=jd,
        role=role,
        ext=ext,
        resume=resume,
        source=source,
    )
    maritime = None
    if overlay:
        maritime = maritime_score(
            extracted.get("raw_text") or "",
            parsed,
            pdf_path=pdf_path,
        )
    return {
        "generic": generic,
        "maritime": maritime,
        "overlay": overlay,
        "job_description": jd,
        "role": role,
    }


def score_layers_after_rewrite(
    rewritten: dict,
    extracted_after: dict,
    parsed_after: dict,
    *,
    job_description: str = "",
    career_track: str = "",
    target_rank: str = "",
    target_cruise_line: str = "",
    overlay: bool = False,
    ext: str = "docx",
) -> dict:
    role = (target_rank or rewritten.get("rank") or parsed_after.get("rank") or "").strip()
    generic = score_generic_rewritten(
        rewritten,
        extracted_after,
        job_description=job_description,
        role=role,
        ext=ext,
    )
    maritime = None
    if overlay:
        maritime = maritime_score(extracted_after.get("raw_text") or "", parsed_after)
    return {
        "generic": generic,
        "maritime": maritime,
        "overlay": overlay,
        "job_description": job_description,
        "role": role,
    }


def maritime_stub_for_profile(parsed: dict) -> dict:
    """Shape CandidateProfile expects when Layer 2 did not run."""
    return {
        "rank_detected": parsed.get("rank") or "unknown",
        "license_rank": "",
        "sea_months": 0,
        "certificates": [],
        "missing_certs": [],
        "vessel_types": [],
    }


def rewrite_quality_from_improvement(improvement: int) -> str:
    if improvement >= REWRITE_IMPROVEMENT_RETRY_POINTS:
        return "full"
    if improvement > 0:
        return "partial"
    return "none"


def should_retry_rewrite(improvement: int, rewrite_failed: bool) -> bool:
    if rewrite_failed:
        return False
    return improvement < REWRITE_IMPROVEMENT_RETRY_POINTS
