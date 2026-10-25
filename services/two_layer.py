"""Two-layer scoring: Resume Matcher generic ATS + optional maritime overlay.

Layer 1 always runs. Layer 2 (certificates, sea service, rank, cruise-line
JD mapping) runs only when career_track / MYC rank fields say it should.
"""

from __future__ import annotations

from config import REWRITE_IMPROVEMENT_RETRY_POINTS
from generic_ats.career_track import maritime_overlay_active, resolve_target_jd
from generic_ats.layer import score_generic, score_generic_rewritten
from pipeline.maritime import detect_rank, maritime_score


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
