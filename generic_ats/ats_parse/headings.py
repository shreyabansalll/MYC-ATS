"""Standard section headings — from Resume Matcher ats_parse/headings.py (en)."""

HEADINGS: dict[str, dict[str, tuple[str, ...]]] = {
    "en": {
        "summary": (
            "summary",
            "profile",
            "professional summary",
            "about me",
            "objective",
        ),
        "experience": (
            "experience",
            "work experience",
            "professional experience",
            "employment history",
            "work history",
            "employment",
            "sea service",
            "sea service record",
        ),
        "education": ("education", "academic background", "qualifications"),
        "skills": ("skills", "technical skills", "core competencies", "competencies"),
        "projects": ("projects", "personal projects"),
        "certifications": (
            "certifications",
            "certificates",
            "licenses",
            "stcw",
        ),
    },
}

REQUIRED_SECTIONS = ("experience", "education", "skills")


def synonyms(kind: str, locale: str | None) -> tuple[str, ...]:
    base = (locale or "").split("-")[0].lower()
    locales = [base, "en"] if base in HEADINGS else list(HEADINGS)
    return tuple(
        s for loc in dict.fromkeys(locales) for s in HEADINGS[loc].get(kind, ())
    )
