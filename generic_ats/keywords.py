"""Deterministic JD keyword extraction (no LLM).

Resume Matcher extracts keywords via LLM (EXTRACT_KEYWORDS_PROMPT). This
service cannot depend on an LLM for scoring — /ats/score is provider-free
by contract. Stopword-filtered token extraction plus whole-phrase skills
gives the same job_keywords dict shape compute_ats_score() expects.
"""

import re

from generic_ats.text_match import keyword_in_text

_STOP = {
    "the", "and", "for", "are", "was", "with", "this", "that", "have",
    "from", "they", "will", "been", "has", "had", "but", "not", "what",
    "all", "were", "when", "your", "can", "said", "there", "use", "each",
    "a", "an", "of", "to", "in", "on", "or", "as", "by", "at", "is",
    "be", "it", "we", "you", "our", "their", "required", "preferred",
    "experience", "must", "should", "including", "plus", "etc",
}

# Used when the caller supplies no job description at all. Resume Matcher
# redistributes weights when keyword_match is None; we still want a
# professional baseline so a no-JD score is comparable across resumes.
GENERIC_PROFESSIONAL_JD = """
Professional seeking a role matching documented experience.
Required: communication, teamwork, problem solving, reporting, documentation.
Preferred: leadership, project management, stakeholder coordination.
"""


def extract_job_keywords(job_description: str, role: str = "") -> dict:
    text = (job_description or "").strip() or GENERIC_PROFESSIONAL_JD
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9+.#/-]{1,}", text)
    keywords: list[str] = []
    seen: set[str] = set()
    for tok in tokens:
        low = tok.lower()
        if low in _STOP or len(low) < 3:
            continue
        if low not in seen:
            seen.add(low)
            keywords.append(tok)
    # Multi-word phrases that look like skills (Title Case pairs)
    for m in re.finditer(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b", text):
        phrase = m.group(1)
        if phrase.lower() not in seen:
            seen.add(phrase.lower())
            keywords.append(phrase)

    inferred_role = role.strip()
    if not inferred_role:
        first = text.strip().split("\n", 1)[0].strip()
        if 3 <= len(first) <= 80:
            inferred_role = first

    required = keywords[:12]
    preferred = keywords[12:20]
    return {
        "role": inferred_role,
        "required_skills": required,
        "preferred_skills": preferred,
        "keywords": keywords[:30],
    }


def keyword_coverage(resume_text: str, job_keywords: dict) -> tuple[float, list[str]]:
    pool: list[str] = []
    for field in ("required_skills", "preferred_skills", "keywords"):
        for item in job_keywords.get(field) or []:
            if isinstance(item, str) and item.strip() and item not in pool:
                pool.append(item)
    if not pool:
        return 0.0, []
    missing = [k for k in pool if not keyword_in_text(k, resume_text)]
    matched = len(pool) - len(missing)
    return round(100.0 * matched / len(pool), 1), missing
