"""Deterministic vacancy requirement extraction and evidence matching.

This is an explainable overlay on the existing generic and maritime scores;
it does not alter either score or infer that absent resume evidence is false.
"""

from __future__ import annotations

import re

from generic_ats.career_track import ATS_RANK_KEYWORDS
from generic_ats.text_match import keyword_in_text
from pipeline.maritime import detect_rank, extract_certificates
from rules.cert_rules import CERT_MATRIX, _aliases_for_cert, cert_requirements_for_rank
from services.maritime_certificates import resolve_certificate_mentions

_REQUIRED = re.compile(r"\b(required|mandatory|must(?: have)?|minimum)\b", re.I)
_PREFERRED = re.compile(r"\b(preferred|desirable|advantageous|nice to have)\b", re.I)
_SKILL_CATALOG = sorted({
    item for values in ATS_RANK_KEYWORDS.values() for item in values
}, key=len, reverse=True)
_CERT_CATALOG = sorted({
    certificate
    for entry in CERT_MATRIX.values()
    for key in ("mandatory_certs", "preferred_certs", "conditional_certs")
    for certificate in entry.get(key, [])
}, key=len, reverse=True)
_VESSEL_TYPES = (
    "cruise", "passenger", "container", "bulk carrier", "tanker", "lng",
    "lpg", "offshore", "platform supply", "oil and chemical", "chemical",
    "ro-ro", "research", "yacht", "ferry", "general cargo",
)


def _unique(values: list[str]) -> list[str]:
    result, seen = [], set()
    for value in values:
        key = re.sub(r"\W+", " ", value.casefold()).strip()
        if key and key not in seen:
            result.append(value)
            seen.add(key)
    return result


def _evidence_snippet(text: str, term: str) -> str | None:
    for line in (text or "").splitlines():
        if keyword_in_text(term, line):
            safe = line.strip()[:300]
            safe = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "[redacted email]", safe)
            safe = re.sub(r"(?<!\w)\+?\d[\d\s()./-]{7,}\d(?!\w)",
                          lambda m:"[redacted number]" if sum(c.isdigit() for c in m.group())>=10 else m.group(),safe)
            safe = re.sub(
                r"\b(passport|cdc|indos|document|certificate)\s*(?:no\.?|number|#|:)\s*[A-Z0-9/-]+",
                r"\1 [redacted]",
                safe,
                flags=re.I,
            )
            return safe
    return None


def _is_certificate(term: str) -> bool:
    value = term.casefold()
    return any(
        value in alias.casefold() or alias.casefold() in value
        for certificate in _CERT_CATALOG
        for alias in _aliases_for_cert(certificate)
    )


def _explicitly_missing(term: str, text: str) -> bool:
    return bool(re.search(
        rf"(?:not held|do not have|does not have|not available|never obtained)\s+(?:the\s+)?{re.escape(term)}\b|"
        rf"{re.escape(term)}\s+(?:not held|not available)", text or "", re.I,
    ))


def _certificate_snippet(term: str, text: str) -> str | None:
    aliases=_aliases_for_cert(term)
    for alias in sorted(aliases,key=len,reverse=True):
        snippet=_evidence_snippet(text,alias)
        if snippet: return snippet
    return None


def _certificate_explicitly_missing(term: str, text: str) -> bool:
    for alias in _aliases_for_cert(term):
        if _explicitly_missing(alias,text): return True
    return _explicitly_missing(term,text)


def _possible_skill_evidence(term: str, text: str) -> str | None:
    """Flag partial lexical overlap for human review; never use for certificates."""
    tokens = {token.casefold() for token in re.findall(r"[A-Za-z][A-Za-z0-9+.#/-]+", term)}
    if len(tokens) < 2:
        return None
    for line in (text or "").splitlines():
        found = {token.casefold() for token in re.findall(r"[A-Za-z][A-Za-z0-9+.#/-]+", line)}
        overlap = len(tokens & found) / len(tokens)
        if overlap >= 0.5 and overlap < 1:
            return line.strip()[:300]
    return None


def extract_vacancy_requirements(job_description: str) -> dict:
    """Extract known maritime criteria and explicit required/preferred labels."""
    text = (job_description or "").strip()
    if not text:
        return {"provided": False, "role": "", "required": [], "preferred": [], "skills": [], "certificates": [], "vessel_types": [], "experience_years": None, "structured_requirements": [], "unresolved": []}

    role = next((line.strip(" :-\t") for line in text.splitlines() if line.strip()), "")[:120]
    required_lines, preferred_lines = [], []
    section = None
    for line in text.splitlines():
        stripped = line.strip().lstrip("-*• ")
        if not stripped:
            section = None
            continue
        if re.fullmatch(r"(?:mandatory|required)(?:\s+(?:skills|qualifications|requirements))?\s*: ?", stripped, re.I):
            section = "required"
            continue
        if re.fullmatch(r"preferred(?:\s+(?:skills|qualifications|requirements))?\s*: ?", stripped, re.I):
            section = "preferred"
            continue
        if _REQUIRED.search(line):
            required_lines.append(line)
            section = "required"
        if _PREFERRED.search(line):
            preferred_lines.append(line)
            section = "preferred"
        elif section == "required":
            required_lines.append(line)
        elif section == "preferred":
            preferred_lines.append(line)
    required_text = "\n".join(required_lines)
    preferred_text = "\n".join(preferred_lines)

    def found_terms(catalog: tuple[str, ...] | list[str], source: str) -> list[str]:
        return _unique([term for term in catalog if keyword_in_text(term, source)])

    def found_certs(source: str) -> list[str]:
        values=found_terms(_CERT_CATALOG,source)
        values.extend(m['official_name'] for m in resolve_certificate_mentions(source)
                      if m.get('status')=='evidenced' and m.get('official_name'))
        values=_unique(values)
        # Prefer a specific catalog identity when the same source line also
        # contains its generic abbreviation (for example GMDSS + GOC).
        return [value for value in values if not any(
            value.casefold() != other.casefold()
            and value.casefold() in other.casefold()
            for other in values
        )]

    certificates = found_certs(text)
    skills = [item for item in found_terms(_SKILL_CATALOG, text) if not _is_certificate(item)]
    vessel_types = found_terms(_VESSEL_TYPES, text)
    required = _unique(
        found_certs(required_text)
        + found_terms(_SKILL_CATALOG, required_text)
        + found_terms(_VESSEL_TYPES, required_text)
    )
    preferred = _unique(
        found_certs(preferred_text)
        + found_terms(_SKILL_CATALOG, preferred_text)
        + found_terms(_VESSEL_TYPES, preferred_text)
    )
    years = re.search(r"\b(?:minimum\s+of\s+)?(\d+(?:\.\d+)?)\s*\+?\s+years?\b", text, re.I)
    rank = detect_rank(text)
    if rank == "unknown":
        rank = ""
    structured=[]; unresolved=[]
    active_priority='unclear'
    for raw_line in text.splitlines():
        line=raw_line.strip().lstrip('-*• ').strip()
        if not line: continue
        if re.fullmatch(r'(?:mandatory|required)(?:\s+(?:skills|qualifications|requirements))?\s*:?',line,re.I):
            active_priority='mandatory'; continue
        if re.fullmatch(r'preferred(?:\s+(?:skills|qualifications|requirements))?\s*:?',line,re.I):
            active_priority='preferred'; continue
        if _REQUIRED.search(line): active_priority='mandatory'
        elif _PREFERRED.search(line): active_priority='preferred'
        line_certs=found_certs(line)
        line_skills=[skill for skill in found_terms(_SKILL_CATALOG,line)
                     if not _is_certificate(skill) and not (
                         detect_rank(line) not in {'unknown',''} and skill.casefold()==detect_rank(line).casefold()
                     )]
        line_vessels=found_terms(_VESSEL_TYPES,line)
        line_rank=detect_rank(line); line_rank='' if line_rank=='unknown' else line_rank
        years_match=re.search(r'\b(\d+(?:\.\d+)?)\s*(years?|months?)\b',line,re.I)
        types=[]
        if line_certs: types.append('certificate')
        if line_rank: types.append('rank')
        if years_match: types.append('experience')
        if line_vessels: types.append('vessel')
        if line_skills: types.append('skill')
        if not types:
            if re.match(r'^(?:[-*•]|must|minimum|required|preferred|at least|valid)',raw_line.strip(),re.I):
                unresolved.append({'text':line,'priority':active_priority,'status':'requires_recruiter_review','source':'job_description'})
            continue
        for req_type,terms in (('certificate',line_certs),('skill',line_skills),('vessel',line_vessels)):
            for term in terms:
                structured.append({'text':term,'type':req_type,'priority':active_priority,
                                   'source_text':line[:240],'status':'extracted','source':'job_description',
                                   'resolution':'known_catalog_match'})
        if line_rank:
            rank_priority='mandatory' if line_rank.casefold()==rank.casefold() and line.strip()==role else active_priority
            structured.append({'text':line_rank,'type':'rank','priority':rank_priority,
                               'source_text':line[:240],'status':'extracted','source':'job_description',
                               'resolution':'deterministic_rank_match'})
        if years_match:
            structured.append({'text':years_match.group(),'type':'experience','priority':active_priority,
                               'source_text':line[:240],'status':'extracted','source':'job_description',
                               'resolution':'numeric_duration'})
    return {
        "provided": True,
        "role": role,
        "rank": rank,
        "required": required,
        "preferred": preferred,
        "skills": skills,
        "certificates": certificates,
        "vessel_types": vessel_types,
        "experience_years": float(years.group(1)) if years else None,
        "structured_requirements": structured,
        "unresolved": unresolved,
    }


def match_vacancy(job_description: str, resume_text: str, parsed: dict | None = None) -> dict:
    requirements = extract_vacancy_requirements(job_description)
    if not requirements["provided"]:
        return {"provided": False, "requirements": requirements, "vacancy_match_score": None,
                "mandatory_eligibility": "not_assessed", "missing_mandatory": [],
                "preferred_satisfied": [], "skill_gaps": {"demonstrated": [], "not_found": [], "possible": [], "requires_verification": []},
                "recommendation": "not_assessed"}

    text = resume_text or ""
    certs = extract_certificates(text)
    certs_norm = " ".join(certs) + "\n" + text
    profile_text = " ".join((parsed or {}).get("skills") or []) + "\n" + text
    mandatory, preferred_satisfied, skill_items = [], [], []
    if requirements.get("rank"):
        candidate_rank = (parsed or {}).get("rank") or detect_rank(text)
        rank_ok = bool(candidate_rank and candidate_rank.casefold() != "unknown" and
                       (candidate_rank.casefold() == requirements["rank"].casefold() or
                        keyword_in_text(requirements["rank"], candidate_rank)))
        mandatory.append({
            "requirement": f"Rank: {requirements['rank']}",
            "type": "rank",
            "status": "demonstrated" if rank_ok else "not_found",
            "evidence": _evidence_snippet(text, candidate_rank) if rank_ok else None,
            "confidence": "high" if rank_ok else "unknown",
        })
        skill_items.append({
            "requirement": f"Rank: {requirements['rank']}",
            "status": "demonstrated" if rank_ok else "not_found",
            "evidence": _evidence_snippet(text, candidate_rank) if rank_ok else None,
            "confidence": "high" if rank_ok else "unknown",
        })
    for term in requirements["required"]:
        is_cert = term in requirements["certificates"] or _is_certificate(term)
        haystack = certs_norm if is_cert else profile_text
        snippet = _certificate_snippet(term,text) if is_cert else _evidence_snippet(haystack, term)
        explicit_missing = _certificate_explicitly_missing(term,text) if is_cert else _explicitly_missing(term, text)
        possible = None if is_cert or explicit_missing or snippet else _possible_skill_evidence(term, profile_text)
        state = "confirmed_missing" if explicit_missing else "demonstrated" if snippet else "possible" if possible else "not_found"
        if explicit_missing:
            snippet = _evidence_snippet(text, term)
        elif possible:
            snippet = _evidence_snippet(text, term) or possible
        confidence = "high" if state in {"demonstrated", "confirmed_missing"} else "medium" if state == "possible" else "unknown"
        mandatory.append({"requirement": term, "type": "certificate" if is_cert else "skill_or_experience", "status": state, "evidence": snippet, "confidence": confidence})
        skill_items.append({"requirement": term, "status": state, "evidence": snippet, "confidence": confidence})
    for term in requirements["preferred"]:
        is_cert = term in requirements["certificates"] or _is_certificate(term)
        snippet = _certificate_snippet(term,text) if is_cert else _evidence_snippet(profile_text, term)
        possible = None if is_cert or snippet else _possible_skill_evidence(term, profile_text)
        if snippet:
            preferred_satisfied.append({"requirement": term, "evidence": snippet})
        elif possible:
            snippet = _evidence_snippet(text, term) or possible
        state = "demonstrated" if snippet and not possible else "possible" if possible else "not_found"
        skill_items.append({"requirement": term, "status": state, "preferred": True, "evidence": snippet, "confidence": "high" if state == "demonstrated" else "medium" if state == "possible" else "unknown"})

    cert_state = {}
    rank = (parsed or {}).get("rank") or ""
    if rank:
        rank_reqs = cert_requirements_for_rank(rank)
        cert_state = rank_reqs
    confirmed_missing = [x for x in mandatory if x["type"] == "certificate" and x["status"] == "confirmed_missing"]
    if confirmed_missing:
        eligibility = "not_eligible_confirmed_missing"
        recommendation = "not_recommended_pending_mandatory_requirements"
    elif any(x["status"] in {"not_found", "possible"} for x in mandatory):
        eligibility = "needs_review"
        recommendation = "review_required"
    elif mandatory:
        eligibility = "evidence_found_review_validity"
        recommendation = "potential_match_review_required"
    else:
        eligibility = "not_assessed"
        recommendation = "insufficient_structured_requirements"

    # Required criteria are represented in both mandatory_requirements and
    # skill_gaps.items; count each criterion only once in the advisory score.
    requirements_count = len(skill_items)
    demonstrated = sum(item["status"] == "demonstrated" for item in skill_items)
    score = round(100 * demonstrated / requirements_count, 1) if requirements_count else None
    missing = [x["requirement"] for x in mandatory if x["status"] == "not_found"]
    return {
        "provided": True,
        "requirements": requirements,
        "vacancy_match_score": score,
        "mandatory_requirements": mandatory,
        "missing_mandatory": missing,
        "preferred_satisfied": preferred_satisfied,
        "skill_gaps": {
            "demonstrated": [x["requirement"] for x in skill_items if x["status"] == "demonstrated"],
            "not_found": [x["requirement"] for x in skill_items if x["status"] == "not_found"],
            "possible": [],
            "requires_verification": [x["requirement"] for x in skill_items if x["status"] == "possible"],
            "items": skill_items,
        },
        "mandatory_eligibility": eligibility,
        "rank_certificate_guidance": cert_state,
        "recommendation": recommendation,
        "score_note": "Separate deterministic evidence signal; does not alter generic ATS or maritime scores.",
    }
