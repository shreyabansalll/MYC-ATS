"""Compose parse-check + Resume Matcher ATS score into one generic result."""

from __future__ import annotations

from config import SCORE_EXCELLENT, SCORE_GOOD, SCORE_PASS
from generic_ats.adapter import flatten_resume_text, parsed_to_resume, rewritten_to_resume
from generic_ats.ats import compute_ats_score
from generic_ats.ats_parse import build_report, document_from_extracted
from generic_ats.keywords import extract_job_keywords, keyword_coverage


def _grade(total: int) -> str:
    if total >= SCORE_EXCELLENT:
        return "EXCELLENT"
    if total >= SCORE_GOOD:
        return "GOOD"
    if total >= SCORE_PASS:
        return "PASS"
    return "FAIL"


def _issues_from_checks(report) -> list[str]:
    issues = []
    for check in report.checks:
        if check.status == "pass":
            continue
        tag = "FAIL" if check.status == "fail" else "WARN"
        issues.append(
            f"GENERIC_ATS: {check.id.upper()} {tag} "
            f"({check.category}/{check.severity})"
        )
    return issues


def score_generic(
    extracted: dict,
    parsed: dict | None = None,
    job_description: str = "",
    role: str = "",
    ext: str = "pdf",
    resume: dict | None = None,
    source: str = "upload",
) -> dict:
    """Layer 1 score. Works for any resume; no maritime rules.

    Combines Resume Matcher parse-check (extraction+layout, 40%) with
    compute_ats_score (content/keywords, 60%) into a 0–100 integer so the
    existing API's ats_score / grade fields stay the same shape.
    """
    doc = document_from_extracted(extracted, ext)
    report = build_report(doc, source=source, template="classic-single-column")

    if resume is None:
        resume = parsed_to_resume(parsed or {}, title=role)

    job_kw = extract_job_keywords(job_description, role=role)
    resume_text = flatten_resume_text(resume) or (extracted.get("raw_text") or "")
    match_pct, missing = keyword_coverage(resume_text, job_kw)
    ats = compute_ats_score(resume, job_kw, match_pct, missing, [])

    parse_score = report.overall_score
    content_score = ats["overall_score"]
    total = int(round(0.4 * parse_score + 0.6 * content_score))
    total = max(0, min(100, total))

    issues = _issues_from_checks(report)
    recommendations = list(ats.get("recommendations") or [])

    return {
        "total_score": total,
        "grade": _grade(total),
        "breakdown": {
            "parse_check": parse_score,
            "content": content_score,
            "sub_scores": ats["sub_scores"],
        },
        "issues": issues,
        "recommendations": recommendations,
        "keyword_data": {
            "match_pct": match_pct,
            "missing_keywords": ats.get("missing_keywords", []),
            "role": job_kw.get("role") or "",
            "engine": "resume_matcher",
        },
        "extractability": report.extractability,
        "parse_checks": [c.model_dump() for c in report.checks],
    }


def score_generic_rewritten(
    rewritten: dict,
    extracted_after: dict,
    job_description: str = "",
    role: str = "",
    ext: str = "docx",
) -> dict:
    resume = rewritten_to_resume(rewritten, title=role)
    return score_generic(
        extracted_after,
        job_description=job_description,
        role=role,
        ext=ext,
        resume=resume,
        source="render",
    )
