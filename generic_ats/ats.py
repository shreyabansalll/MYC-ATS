"""ATS score computation — from Resume Matcher services/ats.py.

Deterministic. No LLM. Weights and scoring rules are the Resume Matcher
composite (keyword / skills / title / sections / dates), not marine-ats's
original format/sections/keywords 40/20/40 split.
"""

import re
from typing import Any

from generic_ats.text_match import keyword_in_text

WEIGHTS: dict[str, float] = {
    "keyword_match": 0.40,
    "skills_coverage": 0.20,
    "title_match": 0.15,
    "section_completeness": 0.15,
    "date_consistency": 0.10,
}

_SECTION_PATTERNS = {
    "summary": ["summary", "objective", "profile", "about"],
    "experience": ["experience", "work history", "employment", "sea service"],
    "education": ["education", "academic", "degree"],
    "skills": ["skills", "technologies", "competencies", "technical"],
}

_TITLE_IN_HEADLINE = 100.0
_TITLE_IN_SUMMARY = 75.0
_TITLE_OVERLAP_MAX = 50.0
_TITLE_STOPWORDS = frozenset(
    {"a", "an", "and", "the", "of", "for", "in", "to", "at", "&"}
)

_YEAR = r"(?:19|20)\d{2}"
_MONTH = (
    r"(?i:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?"
    r"|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
_DATE_PATTERN = re.compile(
    rf"(?P<iso>\b{_YEAR}[-/]\d{{1,2}}\b)"
    rf"|(?P<numeric>\b\d{{1,2}}[/.-]{_YEAR}\b)"
    rf"|(?P<name_year>\b{_MONTH}\.?,?\s+{_YEAR}\b)"
    rf"|(?P<name_short_year>\b{_MONTH}\.?\s+'\d{{2}}\b)"
    rf"|(?P<year>\b{_YEAR}\b)"
)
_MIN_DATES_FOR_CONSISTENCY = 2


def _extract_all_text(data: dict[str, Any]) -> str:
    parts: list[str] = []

    def _walk(obj: Any) -> None:
        if isinstance(obj, str):
            parts.append(obj)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)
        elif isinstance(obj, dict):
            for v in obj.values():
                _walk(v)

    _walk(data)
    return " ".join(parts)


def _title_tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[^\W_]+", text.lower())
        if token not in _TITLE_STOPWORDS
    }


def _is_section_visible(resume: dict[str, Any], key: str) -> bool:
    for meta in resume.get("sectionMeta") or []:
        if isinstance(meta, dict) and meta.get("key") == key:
            return meta.get("isVisible", True) is not False
    return True


def _rendered_content(resume: dict[str, Any]) -> dict[str, Any]:
    content = {
        key: value
        for key, value in resume.items()
        if key != "sectionMeta" and _is_section_visible(resume, key)
    }
    custom = content.get("customSections")
    if isinstance(custom, dict):
        content["customSections"] = {
            key: value
            for key, value in custom.items()
            if _is_section_visible(resume, key)
        }
    return content


def _compute_skills_coverage(
    resume: dict[str, Any],
    job_keywords: dict[str, Any],
) -> float:
    jd_skills: list[str] = []
    jd_skills.extend(job_keywords.get("required_skills", []))
    jd_skills.extend(job_keywords.get("preferred_skills", []))

    if not jd_skills:
        return 0.0

    content = _rendered_content(resume)
    resume_skills: list[str] = (content.get("additional") or {}).get(
        "technicalSkills", []
    ) or []
    resume_text = _extract_all_text(content).lower()
    resume_skills_lower = {s.lower() for s in resume_skills if isinstance(s, str)}

    matched = 0
    for skill in jd_skills:
        if not isinstance(skill, str):
            continue
        skill_lower = skill.lower()
        if skill_lower in resume_skills_lower or keyword_in_text(skill, resume_text):
            matched += 1

    return min(100.0, (matched / len(jd_skills)) * 100)


def compute_title_match(
    resume: dict[str, Any], job_keywords: dict[str, Any]
) -> float | None:
    role = job_keywords.get("role")
    if not isinstance(role, str) or not role.strip():
        return None

    personal_info = resume.get("personalInfo") or {}
    headline = str(personal_info.get("title") or "")
    summary = (
        str(resume.get("summary") or "")
        if _is_section_visible(resume, "summary")
        else ""
    )

    if keyword_in_text(role, headline.lower()):
        return _TITLE_IN_HEADLINE
    if keyword_in_text(role, summary.lower()):
        return _TITLE_IN_SUMMARY

    role_tokens = _title_tokens(role)
    if not role_tokens:
        return 0.0
    overlap = len(role_tokens & _title_tokens(headline)) / len(role_tokens)
    return round(overlap * _TITLE_OVERLAP_MAX, 1)


def _collect_date_strings(resume: dict[str, Any]) -> list[str]:
    entries: list[Any] = []
    for key in ("workExperience", "education", "personalProjects"):
        if _is_section_visible(resume, key):
            entries.extend(resume.get(key) or [])
    for key, section in (resume.get("customSections") or {}).items():
        if isinstance(section, dict) and _is_section_visible(resume, key):
            entries.extend(section.get("items") or [])
    return [
        entry["years"]
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("years"), str)
    ]


def compute_date_consistency(resume: dict[str, Any]) -> float | None:
    style_counts: dict[str, int] = {}
    for value in _collect_date_strings(resume):
        for match in _DATE_PATTERN.finditer(value):
            style = match.lastgroup
            if style:
                style_counts[style] = style_counts.get(style, 0) + 1

    total = sum(style_counts.values())
    if total < _MIN_DATES_FOR_CONSISTENCY:
        return None
    return round(max(style_counts.values()) / total * 100, 1)


def compute_section_completeness(resume: dict[str, Any]) -> float:
    personal_info = resume.get("personalInfo") or {}
    has_contact = bool(personal_info.get("email") or personal_info.get("phone"))
    sections_found = sum(
        [
            bool(resume.get("summary")) and _is_section_visible(resume, "summary"),
            bool(resume.get("workExperience"))
            and _is_section_visible(resume, "workExperience"),
            bool(resume.get("education")) and _is_section_visible(resume, "education"),
            bool((resume.get("additional") or {}).get("technicalSkills"))
            and _is_section_visible(resume, "additional"),
        ]
    )
    if sections_found:
        return (sections_found + has_contact) / (len(_SECTION_PATTERNS) + 1) * 100

    text = _extract_all_text(_rendered_content(resume)).lower()
    headings_found = sum(
        1 for patterns in _SECTION_PATTERNS.values() if any(p in text for p in patterns)
    )
    if not headings_found:
        return 0.0
    if has_contact:
        return (headings_found + 1) / (len(_SECTION_PATTERNS) + 1) * 100
    return headings_found / len(_SECTION_PATTERNS) * 100


def _in_posting_order(keywords: list[str], job_keywords: dict[str, Any]) -> list[str]:
    rank: dict[str, int] = {}
    for field in ("required_skills", "preferred_skills", "keywords"):
        for keyword in job_keywords.get(field) or []:
            if isinstance(keyword, str):
                rank.setdefault(keyword.lower(), len(rank))
    return sorted(keywords, key=lambda k: (rank.get(k.lower(), len(rank)), k.lower()))


def _generate_recommendations(
    sub_scores: dict[str, float | None],
    role: str,
    missing_keywords: list[str],
    injectable_keywords: list[str],
) -> list[str]:
    tips: list[str] = []
    keyword_score = sub_scores["keyword_match"] or 0.0
    skills_score = sub_scores["skills_coverage"] or 0.0
    section_score = sub_scores["section_completeness"] or 0.0
    title_score = sub_scores["title_match"]
    date_score = sub_scores["date_consistency"]

    if title_score is not None and title_score < _TITLE_IN_HEADLINE and role:
        tips.append(
            f'Use the exact job title "{role}" in your resume headline. '
            "Recruiters filter by title literally, so synonyms are missed."
        )

    if keyword_score < 60 and missing_keywords:
        top = ", ".join(missing_keywords[:5])
        tips.append(f"Add these high-priority missing keywords: {top}.")

    if injectable_keywords:
        top_injectable = ", ".join(injectable_keywords[:5])
        tips.append(
            "The following skills are in the source resume but not in this "
            f"version — consider adding them: {top_injectable}."
        )

    if skills_score < 60:
        tips.append(
            "Expand your Skills section to include more of the tools listed "
            "in the job description, only if they already appear in your experience."
        )

    if section_score < 100:
        tips.append(
            "Make sure your resume shows contact details and visible Summary, "
            "Work Experience, Education, and Skills sections."
        )

    if date_score is not None and date_score < 100:
        tips.append(
            'Write every date range in one format (e.g. "Jan 2020 - Mar 2023"). '
            "Mixed formats can make an ATS miscount your years of experience."
        )

    if keyword_score >= 80 and skills_score >= 80:
        tips.append(
            "Strong keyword and skills alignment. Quantify achievements with "
            "metrics already present in the source resume."
        )

    if not tips:
        tips.append(
            "Your resume is well-aligned with the job description."
        )

    return tips


def compute_ats_score(
    refined_resume: dict[str, Any],
    job_keywords: dict[str, Any],
    keyword_match_percentage: float | None,
    missing_keywords: list[str],
    injectable_keywords: list[str],
) -> dict[str, Any]:
    missing_keywords = _in_posting_order(missing_keywords, job_keywords)
    injectable_keywords = _in_posting_order(injectable_keywords, job_keywords)
    sub_scores: dict[str, float | None] = {
        "keyword_match": (
            min(100.0, max(0.0, keyword_match_percentage))
            if keyword_match_percentage is not None
            else None
        ),
        "skills_coverage": _compute_skills_coverage(refined_resume, job_keywords),
        "title_match": compute_title_match(refined_resume, job_keywords),
        "section_completeness": compute_section_completeness(refined_resume),
        "date_consistency": compute_date_consistency(refined_resume),
    }

    scored = {key: value for key, value in sub_scores.items() if value is not None}
    total_weight = sum(WEIGHTS[key] for key in scored)
    overall = (
        sum(value * WEIGHTS[key] for key, value in scored.items()) / total_weight
        if total_weight
        else 0.0
    )

    return {
        "overall_score": round(overall, 1),
        "sub_scores": {
            key: (round(value, 1) if value is not None else None)
            for key, value in sub_scores.items()
        },
        "missing_keywords": missing_keywords[:10],
        "injectable_keywords": injectable_keywords[:10],
        "recommendations": _generate_recommendations(
            sub_scores,
            str(job_keywords.get("role") or "").strip(),
            missing_keywords,
            injectable_keywords,
        ),
    }
