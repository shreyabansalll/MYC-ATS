# tests/test_scorer.py
# Minimal tests for pipeline/scorer.py — pure functions, no I/O.

from pipeline.scorer import score_format, score_sections, score_keywords, score_resume

BASELINE_TEXT = 'Improved fuel efficiency by 20%.'
FULL_CONTACT = {
    'email': 'test@example.com',
    'phone': '+1 234 567 8900',
    'linkedin': 'linkedin.com/in/test',
}

SUMMARY = (
    'Second officer with 5 years experience. Operated ECDIS and radar systems. '
    'Compliant with COLREGS and ISM Code.'
)
EXPERIENCE = 'Led navigation team, achieved 100% safety record, operated ECDIS.'
EDUCATION = 'BSc Marine Engineering, XYZ Maritime University.'
SKILLS_TEXT = 'STCW, GMDSS, ECDIS'
CERTS = 'Standards of Training and Certification (STCW)'
SKILLS_LIST = ['STCW', 'GMDSS', 'ENG1', 'CoC', 'ECDIS', 'ARPA', 'COLREGS', 'MARPOL', 'SOLAS', 'ISM']


# ── score_format ──────────────────────────────────────────────────────

def test_score_format_clean_resume_gets_full_marks():
    score, issues = score_format(
        {'raw_text': BASELINE_TEXT, 'structural_issues': []},
        {'contact': FULL_CONTACT},
    )
    assert score == 40
    assert issues == []


def test_score_format_deducts_per_structural_issue():
    score, issues = score_format(
        {
            'raw_text': BASELINE_TEXT,
            'structural_issues': ['TABLES_DETECTED', 'CONTENT_IN_HEADER', 'MULTI_COLUMN_LAYOUT_DETECTED'],
        },
        {'contact': FULL_CONTACT},
    )
    # 40 - 8 (tables) - 6 (header) - 8 (multi-column)
    assert score == 18
    assert any('TABLES_DETECTED' in i for i in issues)
    assert any('CONTENT_IN_HEADER' in i for i in issues)
    assert any('MULTI_COLUMN_LAYOUT_DETECTED' in i for i in issues)
    assert len(issues) == 3


def test_score_format_deducts_for_missing_contact_fields():
    score, issues = score_format(
        {'raw_text': BASELINE_TEXT, 'structural_issues': []},
        {'contact': {}},
    )
    # 40 - 5 (email) - 3 (phone) - 2 (linkedin)
    assert score == 30
    assert any('EMAIL_NOT_FOUND' in i for i in issues)
    assert any('PHONE_NOT_FOUND' in i for i in issues)
    assert any('LINKEDIN_URL_MISSING' in i for i in issues)


def test_score_format_never_goes_below_zero():
    score, _ = score_format(
        {
            'raw_text': '',
            'structural_issues': ['SCANNED_PDF_DETECTED_OCR_NEEDED', 'UNSUPPORTED_FILE_TYPE'],
        },
        {'contact': {}},
    )
    assert score == 0


# ── score_sections — dual-path (parsed dict vs raw-text re-score) ──────

def test_score_sections_parsed_dict_path():
    parsed = {
        'sections': {
            'summary': SUMMARY,
            'experience': EXPERIENCE,
            'education': EDUCATION,
            'skills': SKILLS_TEXT,
            'certifications': CERTS,
        },
        'skills': SKILLS_LIST,
    }
    score, issues = score_sections(parsed, '')
    assert score == 20
    assert issues == []


def test_score_sections_raw_text_path_matches_generated_docx_headings():
    """
    score_sections falls back to scanning raw_text for the ALL-CAPS
    headings generator.py actually writes — this is what makes re-scoring
    a generated DOCX (no parsed['sections'] dict available) work.
    """
    raw_text = (
        f'PROFESSIONAL SUMMARY\n{SUMMARY}\n\n'
        f'WORK EXPERIENCE\n{EXPERIENCE}\n\n'
        f'EDUCATION\n{EDUCATION}\n\n'
        f'SKILLS\n{SKILLS_TEXT}\n\n'
        f'CERTIFICATIONS\n{CERTS}\n'
    )
    parsed_empty = {'sections': {}, 'skills': SKILLS_LIST}
    score, issues = score_sections(parsed_empty, raw_text)
    assert score == 20
    assert issues == []


def test_score_sections_both_paths_agree():
    parsed = {
        'sections': {
            'summary': SUMMARY,
            'experience': EXPERIENCE,
            'education': EDUCATION,
            'skills': SKILLS_TEXT,
            'certifications': CERTS,
        },
        'skills': SKILLS_LIST,
    }
    dict_score, dict_issues = score_sections(parsed, '')

    raw_text = (
        f'PROFESSIONAL SUMMARY\n{SUMMARY}\n\n'
        f'WORK EXPERIENCE\n{EXPERIENCE}\n\n'
        f'EDUCATION\n{EDUCATION}\n\n'
        f'SKILLS\n{SKILLS_TEXT}\n\n'
        f'CERTIFICATIONS\n{CERTS}\n'
    )
    raw_score, raw_issues = score_sections({'sections': {}, 'skills': SKILLS_LIST}, raw_text)

    assert dict_score == raw_score
    assert dict_issues == raw_issues


def test_score_sections_flags_missing_sections():
    score, issues = score_sections({'sections': {}, 'skills': []}, '')
    assert score == 0
    assert any('SUMMARY_MISSING' in i for i in issues)
    assert any('EXPERIENCE_MISSING' in i for i in issues)
    assert any('LOW_SKILL_COUNT' in i for i in issues)


# ── score_keywords ───────────────────────────────────────────────────

def test_score_keywords_no_job_description_returns_base_score():
    pts, data = score_keywords('some resume text', '')
    assert pts == 20
    assert data['match_pct'] is None


def test_score_keywords_with_job_description_returns_bounded_score():
    pts, data = score_keywords(
        'Second officer with STCW GMDSS ENG1 certification and ECDIS experience.',
        'Seeking second officer with STCW and ECDIS experience.',
    )
    assert 0 <= pts <= 40
    assert 'match_pct' in data


# ── score_resume — full pipeline, grade thresholds ──────────────────────

def test_score_resume_combines_components_and_grades_correctly():
    extracted = {'raw_text': BASELINE_TEXT, 'structural_issues': []}
    parsed = {
        'contact': FULL_CONTACT,
        'sections': {
            'summary': SUMMARY,
            'experience': EXPERIENCE,
            'education': EDUCATION,
            'skills': SKILLS_TEXT,
            'certifications': CERTS,
        },
        'skills': SKILLS_LIST,
    }
    result = score_resume(extracted, parsed, '')
    assert result['breakdown'] == {'format': 40, 'sections': 20, 'keywords': 20}
    assert result['total_score'] == 80
    assert result['grade'] == 'GOOD'  # 75 <= 80 < 85
