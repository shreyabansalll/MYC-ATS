"""Two-layer scoring: Resume Matcher generic ATS + optional maritime overlay.

Layer 1 always runs. Layer 2 (certificates, sea service, rank, cruise-line
JD mapping) runs only when career_track / MYC rank fields say it should.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime

from config import REWRITE_IMPROVEMENT_RETRY_POINTS
from config import CERT_EXPIRING_SOON_DAYS
from generic_ats.adapter import flatten_resume_text, parsed_to_resume
from generic_ats.career_track import (
    company_ats_platform,
    department_for_rank,
    maritime_overlay_active,
    resolve_target_jd,
    score_company_keywords,
    score_rank_keywords,
)
from generic_ats.keywords import extract_job_keywords
from generic_ats.layer import score_generic, score_generic_rewritten
from generic_ats.text_match import keyword_in_text
from pipeline.maritime import (
    detect_rank,
    detect_vessel_types,
    extract_certificates,
    maritime_score,
)
from rules.cert_rules import (
    cert_requirements_for_rank,
    check_cert_evidence,
    normalize_cert_rank,
)
from services.ats_format_checker import check_ats_format_compliance
from services.certificate_validity import track_certificate_validity
from services.semantic_matcher import semantic_skill_match
from services.vacancy_match import _is_certificate, match_vacancy
from services.maritime_intelligence import analyze_maritime_candidate
from services.maritime_certificates import resolve_certificate_mentions
from pipeline.entity_evidence import extract_maritime_entities


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
AI_REWRITE_INTEGRITY_ISSUE = (
    'AI rewrite introduced unsupported material facts; '
    'source-derived content was returned for human review'
)
_FACT_DATE_RE = re.compile(
    r"\b(?:\d{1,2}[/-]\d{4}|20\d{2}-\d{2}-\d{2}|"
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?)\s+\d{4}|(?:19|20)\d{2})\b",
    re.IGNORECASE,
)
_SERVICE_DATE_RANGE_RE = re.compile(
    r"(?P<start>\d{1,2}[/-]\d{4}|20\d{2}-\d{2}-\d{2}|"
    r"[A-Za-z]{3,9}\s+\d{4})\s*[-–]\s*"
    r"(?P<end>\d{1,2}[/-]\d{4}|20\d{2}-\d{2}-\d{2}|"
    r"[A-Za-z]{3,9}\s+\d{4}|present|current)",
    re.IGNORECASE,
)
_VESSEL_NAME_RE = re.compile(
    r"\b(?:M\.?\s*/?\s*[TV]|Vessel(?:\s+Name)?\s*[:=-])\s*"
    r"([A-Z][A-Za-z0-9'(). -]{2,45})",
    re.IGNORECASE,
)


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


_ELIGIBILITY_RANKS = {
    'master': ('deck', 0),
    'staff captain': ('deck', 1),
    'chief officer': ('deck', 2),
    'second officer': ('deck', 3),
    'third officer': ('deck', 4),
    'junior officer': ('deck', 5),
    'deck cadet': ('deck', 6),
    'chief engineer': ('engine', 0),
    'first engineer': ('engine', 1),
    'second engineer': ('engine', 2),
    'third engineer': ('engine', 3),
    'fourth engineer': ('engine', 4),
    'engine cadet': ('engine', 5),
    'chief eto': ('eto', 0),
    'first eto': ('eto', 1),
    'second eto': ('eto', 2),
    'third eto': ('eto', 3),
    'eto': ('eto', 3),
    'electrical cadet': ('eto', 4),
}
_ELIGIBILITY_RANK_ALIASES = {
    'captain': 'master',
    'master mariner': 'master',
    'chief mate': 'chief officer',
    'first officer': 'chief officer',
    '1st officer': 'chief officer',
    '2nd officer': 'second officer',
    '3rd officer': 'third officer',
    '1st engineer': 'first engineer',
    'senior 1st engineer': 'first engineer',
    'hotel engineer': 'first engineer',
    'first assistant engineer': 'second engineer',
    '2nd engineer': 'second engineer',
    '3rd engineer': 'third engineer',
    '4th engineer': 'fourth engineer',
    '1st eto': 'first eto',
    '2nd eto': 'second eto',
    '3rd eto': 'third eto',
}
_ELIGIBILITY_DISPLAY = {
    'master': 'master',
    'staff captain': 'staff captain',
    'chief officer': 'chief officer',
    'second officer': 'second officer',
    'third officer': 'third officer',
    'junior officer': 'junior officer',
    'deck cadet': 'deck cadet',
    'chief engineer': 'chief engineer',
    'first engineer': 'first engineer',
    'second engineer': 'second engineer',
    'third engineer': 'third engineer',
    'fourth engineer': 'fourth engineer',
    'engine cadet': 'engine cadet',
    'chief eto': 'chief ETO',
    'first eto': 'first ETO',
    'second eto': 'second ETO',
    'third eto': 'third ETO',
    'eto': 'third ETO',
    'electrical cadet': 'electrical cadet',
}
_MYC_RANK_POLICY_SOURCE = 'MYC_application_portals.docx — internal placement policy'


def _eligibility_rank(rank: str, raw_text: str = '') -> str:
    value = re.sub(r'\s+', ' ', (rank or '').casefold().replace('_', ' ')).strip()
    value = _ELIGIBILITY_RANK_ALIASES.get(value, value)
    if value in _ELIGIBILITY_RANKS:
        return value
    slug = normalize_cert_rank(value)
    normalized = {
        'chief_officer': 'chief officer',
        'second_officer': 'second officer',
        'third_officer': 'third officer',
        'chief_engineer': 'chief engineer',
        'second_engineer': 'second engineer',
        'third_engineer': 'third engineer',
        'fourth_engineer': 'fourth engineer',
        'engine_cadet': 'engine cadet',
        'deck_cadet': 'deck cadet',
        'eto': 'eto',
        'electrical_cadet': 'electrical cadet',
    }.get(slug, '')
    if normalized:
        return normalized
    if value:
        return ''

    specific_patterns = (
        (r'\b(?:chief|senior)\s+eto\b', 'chief eto'),
        (r'\b1st\s+eto\b|\bfirst\s+eto\b', 'first eto'),
        (r'\b2nd\s+eto\b|\bsecond\s+eto\b', 'second eto'),
        (r'\b3rd\s+eto\b|\bthird\s+eto\b', 'third eto'),
        (r'\b(?:staff|chief)\s+captain\b', 'staff captain'),
        (r'\b(?:senior\s+)?1st\s+engineer\b|\bfirst\s+engineer\b', 'first engineer'),
        (r'\b2nd\s+engineer\b|\bsecond\s+engineer\b', 'second engineer'),
        (r'\b3rd\s+engineer\b|\bthird\s+engineer\b', 'third engineer'),
        (r'\b4th\s+engineer\b|\bfourth\s+engineer\b', 'fourth engineer'),
        (r'\bchief\s+engineer\b', 'chief engineer'),
        (r'\bchief\s+officer\b|\bchief\s+mate\b|\b1st\s+officer\b', 'chief officer'),
        (r'\bsecond\s+officer\b|\b2nd\s+officer\b', 'second officer'),
        (r'\bthird\s+officer\b|\b3rd\s+officer\b', 'third officer'),
        (r'\bmaster mariner\b|\b(?:ship|vessel|sea)\s+captain\b|'
         r"\bship's\s+captain\b|\b(?:rank|position)\s*:\s*master\b", 'master'),
    )
    for line in (raw_text or '').splitlines():
        if len(line) > 160:
            continue
        line = re.sub(r'^\s*[-*•]\s*', '', line)
        for pattern, canonical in specific_patterns:
            match = re.search(pattern, line.casefold())
            prefix = line[:match.start()].strip().casefold() if match else ''
            if match and (match.start() == 0 or prefix in {'rank:', 'position:'}):
                return canonical
    return ''


def _parse_service_date(value: str, *, end: bool = False, today: date | None = None) -> datetime | None:
    value = value.strip()
    if value.casefold() in {'present', 'current'}:
        current = today or datetime.utcnow().date()
        day = calendar.monthrange(current.year, current.month)[1] if end else 1
        return datetime(current.year, current.month, day)
    for fmt in ('%m/%Y', '%m-%Y', '%Y-%m-%d', '%b %Y', '%B %Y'):
        try:
            parsed = datetime.strptime(value, fmt)
            if end and fmt != '%Y-%m-%d':
                parsed = parsed.replace(
                    day=calendar.monthrange(parsed.year, parsed.month)[1],
                )
            return parsed
        except ValueError:
            continue
    return None


def estimate_years_at_rank(
    experience_text: str,
    current_rank: str,
    *,
    today: date | None = None,
) -> float | None:
    """Estimate tenure only from date ranges adjacent to an explicit rank."""
    rank_key = _eligibility_rank(current_rank)
    if not rank_key or not experience_text:
        return None
    lines = experience_text.splitlines()
    periods = []
    today = today or datetime.utcnow().date()
    for index, line in enumerate(lines):
        ranges = list(_SERVICE_DATE_RANGE_RE.finditer(line))
        if not ranges:
            continue
        nearby = '\n'.join(lines[max(0, index - 1):min(len(lines), index + 2)])
        if _eligibility_rank('', nearby) != rank_key:
            continue
        match = ranges[0]
        start = _parse_service_date(match.group('start'), today=today)
        end = _parse_service_date(match.group('end'), end=True, today=today)
        if start is not None and end is not None and end > start:
            periods.append((start, end))
    if not periods:
        return None
    periods.sort()
    merged = [list(periods[0])]
    for start, end in periods[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    total_days = sum((end - start).days for start, end in merged)
    return round(total_days / 365.2425, 2)


def check_rank_eligibility(
    current_rank: str,
    years_at_rank: float | None,
    target_rank: str = '',
) -> dict:
    """Apply MYC's placement recommendation; this is not a regulatory rule."""
    current = _eligibility_rank(current_rank)
    target = _eligibility_rank(target_rank)
    base = {
        'status': 'needs_review',
        'current_rank': _ELIGIBILITY_DISPLAY.get(current) if current else None,
        'years_at_rank': years_at_rank,
        'recommended_rank': None,
        'eligible_for_target': None,
        'basis': 'Rank or rank-specific service dates could not be established; management review is required.',
        'policy_source': _MYC_RANK_POLICY_SOURCE,
        'management_override_allowed': True,
    }
    if not current or years_at_rank is None or years_at_rank < 0:
        return base

    if _ELIGIBILITY_RANKS[current][0] == 'eto':
        recommended = 'third eto'
        basis = 'MYC portal guidance: ETO applications are directed to third ETO, including senior ETOs.'
    elif years_at_rank >= 2.0:
        recommended = current
        basis = 'MYC policy: 2+ years at current rank — apply at the same rank.'
    else:
        department, position = _ELIGIBILITY_RANKS[current]
        ladder = [rank for rank, (rank_department, _) in _ELIGIBILITY_RANKS.items()
                  if rank_department == department]
        ladder.sort(key=lambda rank: _ELIGIBILITY_RANKS[rank][1])
        recommended = ladder[position + 1] if position + 1 < len(ladder) else None
        basis = 'MYC policy: less than 2 years at current rank — consider one rank lower.'
    base.update({
        'status': 'review_recommended',
        'recommended_rank': _ELIGIBILITY_DISPLAY.get(recommended) if recommended else None,
        'eligible_for_target': recommended == target if target else None,
        'basis': basis,
    })
    return base


def _normalize_fact(value: str) -> str:
    return re.sub(r'[^a-z0-9]+', ' ', value.casefold()).strip()


def _metric_fact_key(value: str) -> str:
    normalized = value.casefold()
    for symbol, currency in (('$', 'usd'), ('€', 'eur'), ('£', 'gbp'), ('₹', 'inr')):
        normalized = normalized.replace(symbol, f' {currency} ')
    normalized = normalized.replace('%', ' percent ')
    normalized = re.sub(r'(?<=\d),(?=\d)', '', normalized)
    return re.sub(r'[^a-z0-9.]+', ' ', normalized).strip()


def _metric_facts(text: str) -> set[str]:
    return {
        _metric_fact_key(match.group())
        for match in _METRIC_RE.finditer(text or '')
    }


def _date_facts(text: str) -> set[str]:
    facts = set()
    for match in _FACT_DATE_RE.finditer(text or ''):
        token = match.group().strip()
        parsed = _parse_service_date(token)
        if parsed:
            facts.add(parsed.strftime('%Y-%m'))
        elif re.fullmatch(r'(?:19|20)\d{2}', token):
            facts.add(token)
    return facts


def _experience_text(parsed: dict | None, raw_text: str) -> str:
    sections = (parsed or {}).get('sections') or {}
    return str(sections.get('experience') or raw_text or '')


def _vessel_names(text: str) -> dict[str, str]:
    found = {}
    for match in _VESSEL_NAME_RE.finditer(text or ''):
        name = re.split(r'[,;|/\n]', match.group(1), maxsplit=1)[0].strip(' .-')
        name = re.split(r'\s+(?:GRT|DWT|IMO|GT)\b', name, maxsplit=1, flags=re.IGNORECASE)[0]
        name = re.sub(r'\s+', ' ', name)
        if len(name) < 3:
            continue
        found[_normalize_fact(name)] = name
    return found


def _compare_fact_sets(
    label: str,
    original: set[str],
    rewritten: set[str],
    changes: list[str],
    additions: list[str],
) -> None:
    added = sorted(rewritten - original)
    removed = sorted(original - rewritten)
    if added:
        changes.append(f'{label} added: {", ".join(added)}')
        additions.extend(f'{label}: {value}' for value in added)
    if removed:
        changes.append(f'{label} omitted: {", ".join(removed)}')


def _rewrite_evaluation(
    original_score: int | float | None,
    rewritten_score: int | float,
    original_text: str,
    rewritten_text: str,
    original_parsed: dict | None,
    rewritten_parsed: dict,
    original_maritime: dict | None,
    rewritten_maritime: dict | None,
) -> dict | None:
    if original_score is None:
        return None

    changes: list[str] = []
    additions: list[str] = []
    original_experience = _experience_text(original_parsed, original_text)
    rewritten_experience = _experience_text(rewritten_parsed, rewritten_text)

    original_rank = _eligibility_rank(
        (original_parsed or {}).get('rank') or detect_rank(original_text),
        original_text,
    )
    rewritten_rank = _eligibility_rank(
        rewritten_parsed.get('rank') or detect_rank(rewritten_text),
        rewritten_text,
    )
    if original_rank and rewritten_rank and original_rank != rewritten_rank:
        changes.append(
            f'rank changed: {_ELIGIBILITY_DISPLAY.get(original_rank, original_rank)} -> '
            f'{_ELIGIBILITY_DISPLAY.get(rewritten_rank, rewritten_rank)}'
        )
        additions.append(f'rank: {_ELIGIBILITY_DISPLAY.get(rewritten_rank, rewritten_rank)}')

    _compare_fact_sets(
        'experience dates',
        _date_facts(original_experience),
        _date_facts(rewritten_experience),
        changes,
        additions,
    )
    _compare_fact_sets(
        'quantified experience values',
        _metric_facts(original_experience),
        _metric_facts(rewritten_experience),
        changes,
        additions,
    )

    original_months = (original_maritime or {}).get('sea_months')
    rewritten_months = (rewritten_maritime or {}).get('sea_months')
    if (
        isinstance(original_months, int)
        and isinstance(rewritten_months, int)
        and original_months != rewritten_months
    ):
        changes.append(f'sea_service_months changed: {original_months} -> {rewritten_months}')
        if rewritten_months > original_months:
            additions.append(f'sea_service_months: {rewritten_months}')

    original_vessels = _vessel_names(original_experience)
    rewritten_vessels = _vessel_names(rewritten_experience)
    vessel_additions = set(rewritten_vessels) - set(original_vessels)
    vessel_removals = set(original_vessels) - set(rewritten_vessels)
    if vessel_additions:
        names = [rewritten_vessels[key] for key in sorted(vessel_additions)]
        changes.append(f'vessel names added: {", ".join(names)}')
        additions.extend(f'vessel name: {name}' for name in names)
    if vessel_removals:
        names = [original_vessels[key] for key in sorted(vessel_removals)]
        changes.append(f'vessel names omitted: {", ".join(names)}')

    original_certs = extract_certificates(original_text)
    rewritten_certs = extract_certificates(rewritten_text)
    original_cert_map = {_normalize_fact(cert): cert for cert in original_certs}
    rewritten_cert_map = {_normalize_fact(cert): cert for cert in rewritten_certs}
    certs_added = [
        rewritten_cert_map[key]
        for key in sorted(set(rewritten_cert_map) - set(original_cert_map))
    ]
    certs_lost = [
        original_cert_map[key]
        for key in sorted(set(original_cert_map) - set(rewritten_cert_map))
    ]
    if certs_added:
        changes.append(f'certificates added: {", ".join(certs_added)}')
        additions.extend(f'certificate: {cert}' for cert in certs_added)
    if certs_lost:
        changes.append(f'certificates omitted: {", ".join(certs_lost)}')

    original_cert_facts={_normalize_fact(item) for item in original_certs}
    source_claims=[]
    for mention in resolve_certificate_mentions(rewritten_text):
        if mention.get('official_name'):
            claim=mention['official_name']
            source_claims.append({'type':'certificate','claim':claim,
                                  'source_supported':_normalize_fact(claim) in original_cert_facts,
                                  'verification_status':'resume_text_match_unverified'})
    if rewritten_rank and rewritten_rank != 'unknown':
        source_claims.append({'type':'rank','claim':rewritten_rank,
                              'source_supported':not original_rank or rewritten_rank==original_rank,
                              'verification_status':'resume_text_match_unverified'})
    original_vessel_types={x.casefold() for x in detect_vessel_types(original_experience)}
    rewritten_vessel_types={x.casefold() for x in detect_vessel_types(rewritten_experience)}
    added_vessel_types=sorted(rewritten_vessel_types-original_vessel_types)
    if added_vessel_types:
        changes.append(f'vessel experience types added: {", ".join(added_vessel_types)}')
        additions.extend(f'vessel type: {value}' for value in added_vessel_types)
    for vessel_type in sorted(rewritten_vessel_types):
        source_claims.append({'type':'vessel_experience','claim':vessel_type,
                              'source_supported':vessel_type in original_vessel_types,
                              'verification_status':'resume_text_match_unverified'})
    original_date_facts=_date_facts(original_experience)
    for fact in sorted(_date_facts(rewritten_experience)):
        source_claims.append({'type':'experience_date','claim':fact,
                              'source_supported':fact in original_date_facts,
                              'verification_status':'resume_text_match_unverified'})

    score_delta = float(rewritten_score) - float(original_score)
    facts_preserved = not changes
    if additions:
        verdict = 'factual_integrity_risk'
    elif score_delta < 0 or changes:
        verdict = 'degraded'
    elif score_delta > 0:
        verdict = 'improved'
    else:
        verdict = 'neutral'
    return {
        'original_score': float(original_score),
        'rewritten_score': float(rewritten_score),
        'score_delta': score_delta,
        'score_improved': score_delta > 0,
        'facts_preserved': facts_preserved,
        'facts_changed': changes,
        'certs_lost': certs_lost,
        'certs_added': certs_added,
        'claim_source_map': source_claims,
        'unsupported_claim_count': sum(not claim['source_supported'] for claim in source_claims),
        'verdict': verdict,
    }


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
    raw_text = extracted.get('raw_text') or parsed.get('raw_text') or ''
    company = score_company_keywords(
        target_cruise_line,
        department_for_rank(role),
        raw_text,
    )
    rank_keywords = score_rank_keywords(role, raw_text)
    platform_info = company_ats_platform(target_cruise_line)
    format_check = check_ats_format_compliance(
        raw_text,
        platform_info['platform'],
        structural_issues=extracted.get('structural_issues'),
    )
    format_check['company'] = platform_info['company']
    format_check['platform_assumption'] = platform_info['assumption']
    format_check['platform_source'] = platform_info['source']

    cert_evidence = (
        check_cert_evidence(role, extract_certificates(raw_text), raw_text)
        if overlay else {}
    )
    cert_requirements = cert_requirements_for_rank(role) if overlay else {}
    # Only a caller-supplied vacancy is a vacancy. The generic/rank template
    # used by the ATS keyword score must not masquerade as a specific opening.
    vacancy_match = match_vacancy(job_description, raw_text, parsed)
    vacancy_requirements = vacancy_match.get('requirements') or {}
    semantic_terms = (vacancy_requirements.get('required') or []) + (
        vacancy_requirements.get('preferred') or []
    )
    semantic_match = semantic_skill_match(
        semantic_terms,
        parsed.get('skills') or [],
        certificate_requirements=[term for term in semantic_terms if _is_certificate(term)],
    )
    certificate_validity = track_certificate_validity(
        raw_text, expiring_soon_days=CERT_EXPIRING_SOON_DAYS,
    )
    maritime_entities = extract_maritime_entities(raw_text)
    maritime_intelligence = analyze_maritime_candidate(
        raw_text, detected_rank or role, vacancy_requirements,
    ) if overlay else {}

    rank_eligibility = None
    if overlay:
        eligibility_rank = _eligibility_rank(detected_rank, raw_text)
        experience_text = _experience_text(parsed, raw_text)
        tenure = estimate_years_at_rank(experience_text, eligibility_rank)
        rank_eligibility = check_rank_eligibility(
            eligibility_rank,
            tenure,
            target_rank,
        )
    return {
        "generic": generic,
        "maritime": maritime,
        "overlay": overlay,
        "job_description": jd,
        "role": role,
        "cert_evidence": cert_evidence,
        "cert_requirements": cert_requirements,
        "company_keyword_matches": company['matches'],
        "company_keyword_score": company['score'] if company['company'] else None,
        "company_keyword_company": company['company'],
        "company_keyword_department": company['department'],
        "company_keyword_source": company['source'],
        "rank_keyword_matches": rank_keywords['matches'],
        "rank_keyword_rank": rank_keywords['rank'],
        "rank_keyword_source": rank_keywords['source'],
        "format_check": format_check,
        "rank_eligibility": rank_eligibility,
        "vacancy_match": vacancy_match,
        "semantic_match": semantic_match,
        "certificate_validity": certificate_validity,
        "maritime_entities": maritime_entities,
        "maritime_intelligence": maritime_intelligence,
        "score_explanation": {
            "total_score": generic.get("total_score"),
            "composition": {"parse_points": {"weight": 0.40, "score": generic.get("breakdown", {}).get("parse_points")},
                            "content_points": {"weight": 0.60, "score": generic.get("breakdown", {}).get("content_points")}},
            "content_subscores": generic.get("breakdown", {}).get("sub_scores", {}),
            "note": "Weights describe the generic ATS score; maritime and vacancy signals are separate and do not override mandatory eligibility review.",
        },
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
    original_score: int | float | None = None,
    original_text: str = "",
    original_parsed: dict | None = None,
    original_maritime: dict | None = None,
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
    raw_text = extracted_after.get('raw_text') or parsed_after.get('raw_text') or ''
    company = score_company_keywords(
        target_cruise_line,
        department_for_rank(role),
        raw_text,
    )
    rank_keywords = score_rank_keywords(role, raw_text)
    platform_info = company_ats_platform(target_cruise_line)
    format_check = check_ats_format_compliance(
        raw_text,
        platform_info['platform'],
        structural_issues=extracted_after.get('structural_issues'),
    )
    format_check['company'] = platform_info['company']
    format_check['platform_assumption'] = platform_info['assumption']
    format_check['platform_source'] = platform_info['source']
    cert_evidence = (
        check_cert_evidence(role, extract_certificates(raw_text), raw_text)
        if overlay else {}
    )
    cert_requirements = cert_requirements_for_rank(role) if overlay else {}
    vacancy_match = match_vacancy(job_description, raw_text, parsed_after)
    vacancy_requirements = vacancy_match.get('requirements') or {}
    semantic_terms = (vacancy_requirements.get('required') or []) + (
        vacancy_requirements.get('preferred') or []
    )
    semantic_match = semantic_skill_match(
        semantic_terms,
        parsed_after.get('skills') or [],
        certificate_requirements=[term for term in semantic_terms if _is_certificate(term)],
    )
    certificate_validity = track_certificate_validity(
        raw_text, expiring_soon_days=CERT_EXPIRING_SOON_DAYS,
    )
    maritime_entities = extract_maritime_entities(raw_text)
    maritime_intelligence = analyze_maritime_candidate(
        raw_text, role, vacancy_requirements,
    ) if overlay else {}
    rewrite_evaluation = _rewrite_evaluation(
        original_score,
        generic['total_score'],
        original_text,
        raw_text,
        original_parsed,
        parsed_after,
        original_maritime,
        maritime,
    )
    rank_eligibility = None
    if overlay:
        current_rank = _eligibility_rank(
            (original_parsed or {}).get('rank') or detect_rank(original_text),
            original_text,
        )
        tenure = estimate_years_at_rank(
            _experience_text(original_parsed, original_text),
            current_rank,
        )
        rank_eligibility = check_rank_eligibility(current_rank, tenure, target_rank)
    return {
        "generic": generic,
        "maritime": maritime,
        "overlay": overlay,
        "job_description": job_description,
        "role": role,
        "cert_evidence": cert_evidence,
        "cert_requirements": cert_requirements,
        "company_keyword_matches": company['matches'],
        "company_keyword_score": company['score'] if company['company'] else None,
        "company_keyword_company": company['company'],
        "company_keyword_department": company['department'],
        "company_keyword_source": company['source'],
        "rank_keyword_matches": rank_keywords['matches'],
        "rank_keyword_rank": rank_keywords['rank'],
        "rank_keyword_source": rank_keywords['source'],
        "format_check": format_check,
        "rank_eligibility": rank_eligibility,
        "rewrite_evaluation": rewrite_evaluation,
        "vacancy_match": vacancy_match,
        "semantic_match": semantic_match,
        "certificate_validity": certificate_validity,
        "maritime_entities": maritime_entities,
        "maritime_intelligence": maritime_intelligence,
        "score_explanation": {
            "total_score": generic.get("total_score"),
            "composition": {"parse_points": {"weight": 0.40, "score": generic.get("breakdown", {}).get("parse_points")},
                            "content_points": {"weight": 0.60, "score": generic.get("breakdown", {}).get("content_points")}},
            "content_subscores": generic.get("breakdown", {}).get("sub_scores", {}),
            "note": "Weights describe the generic ATS score; maritime and vacancy signals are separate and do not override mandatory eligibility review.",
        },
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
