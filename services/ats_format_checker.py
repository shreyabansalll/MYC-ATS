"""Advisory parsing-readiness checks; these do not affect ATS scores."""

from __future__ import annotations

import re


_DATE_PATTERNS = {
    'numeric_month_year': re.compile(r'\b\d{1,2}[/-]\d{4}\b'),
    'month_name_year': re.compile(
        r'\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|'
        r'jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|'
        r'dec(?:ember)?)\s+\d{4}\b',
        re.IGNORECASE,
    ),
    'iso_date': re.compile(r'\b20\d{2}-\d{2}-\d{2}\b'),
    'year_only': re.compile(r'\b(?:19|20)\d{2}\b'),
}

_STANDARD_HEADERS = {
    'summary', 'professional summary', 'profile', 'objective',
    'experience', 'work experience', 'employment history', 'sea service',
    'sea service record', 'skills', 'technical skills', 'certifications',
    'certificates', 'education', 'languages', 'documents', 'license',
    'licence', 'training', 'references', 'contact', 'contact information',
}


def _looks_like_multi_column(resume_text: str) -> bool:
    lines = [line.strip() for line in (resume_text or '').splitlines() if line.strip()]
    short_lines = sum(len(line) < 40 for line in lines)
    long_lines = sum(len(line) > 80 for line in lines)
    return short_lines >= 3 and long_lines >= 2


def _date_formats(resume_text: str) -> set[str]:
    found = set()
    specific_spans = []
    for label, pattern in _DATE_PATTERNS.items():
        matches = list(pattern.finditer(resume_text or ''))
        if matches and label != 'year_only':
            found.add(label)
            specific_spans.extend(match.span() for match in matches)
    year_pattern = _DATE_PATTERNS['year_only']
    if any(
        not any(start <= match.start() and match.end() <= end for start, end in specific_spans)
        for match in year_pattern.finditer(resume_text or '')
    ):
        found.add('year_only')
    return found


def _has_inconsistent_dates(resume_text: str) -> bool:
    return len(_date_formats(resume_text)) > 1


def _dates_match_mmm_yyyy(resume_text: str) -> bool:
    text = resume_text or ''
    formats = _date_formats(text)
    if not formats:
        return False
    return formats == {'month_name_year'}


def _has_exact_skills_header(resume_text: str) -> bool:
    return any(
        line.strip().casefold() == 'skills'
        for line in (resume_text or '').splitlines()
    )


def _has_custom_section_headers(resume_text: str) -> bool:
    for line in (resume_text or '').splitlines():
        value = line.strip().rstrip(':').strip()
        if not line.strip().endswith(':') or len(value) > 50:
            continue
        normalized = re.sub(r'\s+', ' ', value.casefold())
        if normalized and normalized not in _STANDARD_HEADERS:
            return True
    return False


def check_ats_format_compliance(
    resume_text: str,
    ats_platform: str,
    *,
    structural_issues: list[str] | None = None,
) -> dict:
    """Return practical parsing-readiness flags, not vendor guarantees."""
    platform = (ats_platform or 'unknown').casefold()
    warnings = []
    errors = []
    known_column_signal = any(
        'MULTI_COLUMN' in str(issue).upper()
        for issue in (structural_issues or [])
    )

    if known_column_signal or _looks_like_multi_column(resume_text):
        finding = {
            'code': 'MULTI_COLUMN_LAYOUT',
            'severity': 'warning',
            'reason': (
                'Multi-column layouts can cause reading-order inconsistencies in resume parsers. '
                'This is an advisory pattern flag; employer configurations vary.'
            ),
        }
        if platform in {'successfactors', 'taleo', 'greenhouse'}:
            finding['severity'] = 'high_attention'
            errors.append(finding)
        else:
            warnings.append(finding)

    if platform == 'workday' and _has_inconsistent_dates(resume_text):
        warnings.append({
            'code': 'INCONSISTENT_DATE_FORMAT',
            'severity': 'warning',
            'reason': (
                'Use one date format throughout (for example MM/YYYY) to make chronology '
                'easier to parse; this is a general recommendation, not a vendor guarantee.'
            ),
        })

    if platform == 'successfactors':
        if not _dates_match_mmm_yyyy(resume_text) and _date_formats(resume_text):
            warnings.append({
                'code': 'DATE_FORMAT_MISMATCH',
                'severity': 'warning',
                'reason': (
                    'MYC recommends a consistent MMM YYYY date range (for example Jan 2019 - '
                    'Mar 2022) for parsing readiness; actual employer configuration varies.'
                ),
            })
        if not _has_exact_skills_header(resume_text):
            warnings.append({
                'code': 'NON_STANDARD_SKILLS_HEADER',
                'severity': 'warning',
                'reason': (
                    "Use a standard 'Skills' heading rather than a custom variant to improve "
                    'section recognition; this is advisory, not a guaranteed vendor behavior.'
                ),
            })

    if _has_custom_section_headers(resume_text):
        warnings.append({
            'code': 'CUSTOM_SECTION_HEADERS',
            'severity': 'warning',
            'reason': (
                'A custom section heading may be skipped by resume parsers. Prefer clear labels '
                'such as Summary, Experience, Education, and Skills.'
            ),
        })

    if '—' in (resume_text or ''):
        warnings.append({
            'code': 'EM_DASH_DETECTED',
            'severity': 'warning',
            'reason': (
                'An em dash can render inconsistently in some text extraction paths; a hyphen '
                'or rephrased sentence is a safer formatting choice.'
            ),
        })

    if platform == 'taleo':
        warnings.append({
            'code': 'FILE_FORMAT_ADVISORY',
            'severity': 'info',
            'reason': (
                'MYC reference material recommends considering DOCX for Taleo-associated '
                'applications. This is an operational assumption, not a verified guarantee '
                'about the employer configuration.'
            ),
        })

    return {
        'errors': errors,
        'warnings': warnings,
        'platform': platform,
        'note': (
            'These are advisory parsing-readiness recommendations. ATS mappings are '
            'MYC-supplied operational assumptions; actual employer parser behavior varies.'
        ),
    }
