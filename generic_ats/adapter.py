"""Convert marine-ats parsed/rewritten dicts to Resume Matcher resume JSON."""

from __future__ import annotations

import re
from typing import Any


def parsed_to_resume(parsed: dict, title: str = "") -> dict[str, Any]:
    sections = parsed.get("sections") or {}
    contact = parsed.get("contact") or {}
    exp_text = sections.get("experience") or ""
    bullets = [ln.strip(" •-\t") for ln in exp_text.splitlines() if ln.strip()][:24]
    years = _first_date_range(exp_text)
    education_text = (sections.get("education") or "").strip()
    certs = [
        ln.strip(" •-\t")
        for ln in (sections.get("certifications") or "").splitlines()
        if ln.strip()
    ]
    langs = [
        ln.strip(" •-\t")
        for ln in (sections.get("languages") or "").splitlines()
        if ln.strip()
    ]
    return {
        "personalInfo": {
            "name": parsed.get("name") or "",
            "title": title or parsed.get("rank") or "",
            "email": contact.get("email") or "",
            "phone": contact.get("phone") or "",
            "linkedin": contact.get("linkedin") or "",
        },
        "summary": (sections.get("summary") or "").strip(),
        "workExperience": (
            [
                {
                    "id": 1,
                    "title": title or parsed.get("rank") or "Professional",
                    "company": "",
                    "years": years,
                    "description": bullets or ([exp_text.strip()] if exp_text.strip() else []),
                }
            ]
            if exp_text.strip()
            else []
        ),
        "education": (
            [
                {
                    "id": 1,
                    "institution": education_text[:200],
                    "degree": "",
                    "years": _first_date_range(education_text),
                }
            ]
            if education_text
            else []
        ),
        "additional": {
            "technicalSkills": list(parsed.get("skills") or []),
            "languages": langs,
            "certificationsTraining": certs,
        },
        "customSections": {},
    }


def rewritten_to_resume(rewritten: dict, title: str = "") -> dict[str, Any]:
    contact = rewritten.get("contact") or {}
    work = []
    for i, job in enumerate(rewritten.get("experience_bullets") or [], start=1):
        if isinstance(job, str):
            work.append(
                {
                    "id": i,
                    "title": title or "Professional",
                    "company": "",
                    "years": "",
                    "description": [job],
                }
            )
            continue
        if not isinstance(job, dict):
            continue
        work.append(
            {
                "id": i,
                "title": job.get("role") or title or "Professional",
                "company": job.get("company") or "",
                "years": job.get("dates") or "",
                "description": [
                    str(b) for b in (job.get("bullets") or []) if str(b).strip()
                ],
            }
        )
    education = []
    for i, row in enumerate(rewritten.get("education") or [], start=1):
        if isinstance(row, str):
            education.append(
                {"id": i, "institution": row, "degree": "", "years": ""}
            )
        elif isinstance(row, dict):
            education.append(
                {
                    "id": i,
                    "institution": row.get("institution") or row.get("school") or "",
                    "degree": row.get("degree") or "",
                    "years": row.get("years") or row.get("dates") or "",
                }
            )
    certs = []
    for c in rewritten.get("certifications") or []:
        certs.append(c if isinstance(c, str) else str(c))
    return {
        "personalInfo": {
            "name": rewritten.get("name") or "",
            "title": rewritten.get("rank") or title or "",
            "email": contact.get("email") or "",
            "phone": contact.get("phone") or "",
            "linkedin": contact.get("linkedin") or "",
        },
        "summary": rewritten.get("summary") or "",
        "workExperience": work,
        "education": education,
        "additional": {
            "technicalSkills": list(rewritten.get("skills") or []),
            "languages": list(rewritten.get("languages") or []),
            "certificationsTraining": certs,
        },
        "customSections": {},
    }


def flatten_resume_text(resume: dict) -> str:
    parts: list[str] = []

    def walk(obj):
        if isinstance(obj, str):
            parts.append(obj)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)
        elif isinstance(obj, dict):
            for v in obj.values():
                walk(v)

    walk(resume)
    return "\n".join(parts)


def _first_date_range(text: str) -> str:
    m = re.search(
        r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{4}"
        r"|\d{1,2}/\d{4}|\d{4})\s*[-–]\s*"
        r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{4}"
        r"|\d{1,2}/\d{4}|\d{4}|Present|Current)",
        text or "",
        re.I,
    )
    return m.group(0) if m else ""
