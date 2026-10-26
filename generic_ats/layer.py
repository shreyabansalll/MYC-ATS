"""Compose parse-check + Resume Matcher ATS score into one generic result."""

from __future__ import annotations

import re

from config import SCORE_EXCELLENT, SCORE_GOOD, SCORE_PASS
from generic_ats.adapter import flatten_resume_text, parsed_to_resume, rewritten_to_resume
from generic_ats.ats import WEIGHTS, compute_ats_score
from generic_ats.ats_parse import build_report, document_from_extracted
from generic_ats.keywords import extract_job_keywords, keyword_coverage
from generic_ats.text_match import keyword_in_text
from generic_ats.writing_quality import numeric_claims


_WORD_RE = re.compile(r"\b[\w'-]+\b")
_GENERIC_OBJECTIVE_RE = re.compile(
    r"\b(?:objective|seeking|looking for|to obtain|to secure|"
    r"desire(?:d)?\s+(?:a|an)?\s*position)\b",
    re.IGNORECASE,
)
_GENERIC_OBJECTIVE_TERMS = frozenset(
    {"experience", "job", "looking", "position", "professional", "role", "seeking"}
)
_VAGUE_DUTY_RE = re.compile(r"^\W*(?:managed|assisted|helped|supported)\b", re.IGNORECASE)
_OUTCOME_RE = re.compile(
    r"\b(?:improv\w*|reduc\w*|increas\w*|delivered|achieved|saved|grew|generated|"
    r"completed|streamlined|resolved|result(?:ed)?)\b",
    re.IGNORECASE,
)


def _grade(total: int) -> str:
    if total >= SCORE_EXCELLENT:
        return "EXCELLENT"
    if total >= SCORE_GOOD:
        return "GOOD"
    if total >= SCORE_PASS:
        return "PASS"
    return "FAIL"


def _calibrate_score(raw_score: float) -> float:
    if raw_score < 40:
        return raw_score * 0.75
    if raw_score > 85:
        return 85 + (raw_score - 85) * 0.6
    return raw_score


def _check_params(report, check_id: str) -> dict:
    check = next((item for item in report.checks if item.id == check_id), None)
    return check.params if check else {}


def _recompute_ats_total(sub_scores: dict) -> float:
    scored = {
        key: value for key, value in sub_scores.items() if value is not None
    }
    total_weight = sum(WEIGHTS[key] for key in scored)
    if not total_weight:
        return 0.0
    return round(
        sum(value * WEIGHTS[key] for key, value in scored.items()) / total_weight,
        1,
    )


def _experience_bullets(resume: dict) -> list[str]:
    bullets: list[str] = []
    for entry in resume.get("workExperience") or []:
        if not isinstance(entry, dict):
            continue
        description = entry.get("description") or []
        if isinstance(description, str):
            description = [description]
        bullets.extend(
            item.strip() for item in description if isinstance(item, str) and item.strip()
        )
    return bullets


def _content_score(
    ats_score: float,
    resume: dict,
    report,
    job_keywords: dict,
    raw_text: str,
) -> tuple[float, list[str]]:
    section_params = _check_params(report, "section_headings")
    missing_sections = set(section_params.get("missing") or [])
    found_sections = set(section_params.get("found") or [])
    personal_info = resume.get("personalInfo") or {}
    contact_params = _check_params(report, "contact_info")
    has_contact = bool(
        personal_info.get("email")
        or personal_info.get("phone")
        or contact_params.get("email")
        or contact_params.get("phone")
    )

    bullets = _experience_bullets(resume)
    bullets_text = " ".join(bullets)
    word_count = len(_WORD_RE.findall(raw_text))
    penalties: list[str] = []
    points = ats_score * 0.6

    missing_experience = (
        "experience" in missing_sections or not (resume.get("workExperience") or [])
    )
    missing_skills = (
        "skills" in missing_sections
        or not (resume.get("additional") or {}).get("technicalSkills")
    )
    if missing_skills:
        points -= 10
        penalties.append("GENERIC_ATS: missing skills section (-10 content points)")
    if not has_contact:
        points -= 8
        penalties.append("GENERIC_ATS: missing contact information (-8 content points)")

    if not numeric_claims(bullets_text):
        points -= 8
        penalties.append("GENERIC_ATS: no quantified experience achievements (-8 content points)")

    objective_text = " ".join(
        str(resume.get(key) or "") for key in ("objective", "summary")
    )
    objective_is_specific = bool(
        isinstance(job_keywords.get("role"), str)
        and job_keywords["role"].strip()
        and job_keywords["role"].lower() not in {"professional", "job"}
        and keyword_in_text(job_keywords["role"], objective_text.lower())
    ) or any(
        isinstance(skill, str)
        and skill
        and skill.lower() not in _GENERIC_OBJECTIVE_TERMS
        and keyword_in_text(skill, objective_text.lower())
        for skill in (
            (job_keywords.get("required_skills") or [])
            + (job_keywords.get("preferred_skills") or [])
        )
    )
    if _GENERIC_OBJECTIVE_RE.search(objective_text) and not objective_is_specific:
        points -= 5
        penalties.append("GENERIC_ATS: generic objective statement (-5 content points)")

    if bullets:
        vague_count = sum(
            bool(_VAGUE_DUTY_RE.search(bullet)) and not _OUTCOME_RE.search(bullet)
            for bullet in bullets
        )
        vague_penalty = min(6, round(6 * vague_count / len(bullets)))
        if vague_penalty:
            points -= vague_penalty
            penalties.append(
                f"GENERIC_ATS: vague duties without outcomes (-{vague_penalty} content points)"
            )

    if word_count < 300:
        points *= 0.75
        penalties.append("GENERIC_ATS: thin resume under 300 words (0.75 content multiplier)")
    if missing_experience:
        points = min(points, 30)
        penalties.append("GENERIC_ATS: missing work experience section (content capped at 30/60)")
    if word_count < 150:
        points = min(points, 20)
        penalties.append("GENERIC_ATS: resume under 150 words (content capped at 20/60)")

    return max(0.0, min(60.0, points)), penalties


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
    raw_text = extracted.get("raw_text") or ""
    match_pct, missing = keyword_coverage(resume_text, job_kw)
    ats = compute_ats_score(resume, job_kw, match_pct, missing, [])

    section_params = _check_params(report, "section_headings")
    if not section_params.get("found"):
        ats["sub_scores"]["section_completeness"] = 0.0
        ats["overall_score"] = _recompute_ats_total(ats["sub_scores"])

    parse_points = report.overall_score * 0.4
    table_params = _check_params(report, "tables")
    multi_column_params = _check_params(report, "multi_column")
    if table_params.get("count", 0):
        if any(c.id == "tables" and c.status == "warn" for c in report.checks):
            parse_points += 6 * 0.4
        parse_points -= 15
    if multi_column_params.get("pages"):
        parse_points += 12 * 0.4
        parse_points -= 12
    parse_points = max(0.0, min(40.0, parse_points))
    if report.extractability == "none":
        parse_points = min(parse_points, 5.0)

    content_points, content_penalties = _content_score(
        ats["overall_score"], resume, report, job_kw, raw_text
    )
    raw_total = max(0.0, min(100.0, parse_points + content_points))
    total = int(round(_calibrate_score(raw_total)))

    issues = _issues_from_checks(report)
    issues.extend(content_penalties)
    recommendations = list(ats.get("recommendations") or [])

    return {
        "total_score": total,
        "raw_total_score": round(raw_total, 1),
        "grade": _grade(total),
        "breakdown": {
            "parse_check": round(parse_points / 0.4, 1),
            "parse_points": round(parse_points, 1),
            "content": round(content_points / 0.6, 1),
            "content_points": round(content_points, 1),
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
