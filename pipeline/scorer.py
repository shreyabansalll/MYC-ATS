# pipeline/scorer.py
# STAGE 4 — Full ATS Scoring Engine
# Format (40pts) + Section Completeness (20pts) + Keyword Match (40pts)

import re
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

STOP_WORDS = {
    'the','and','for','are','was','with','this','that','have',
    'from','they','will','been','has','had','but','not','what',
    'all','were','when','your','can','said','there','use','each'
}

PASSIVE_PATTERNS = [
    r'\bwas responsible for\b',
    r'\bwere responsible for\b',
    r'\bhas been\b',
    r'\bhave been\b',
    r'\bwas involved in\b',
    r'\bwas part of\b',
]

PERSONAL_PRONOUNS = [r'\bI\b', r'\bmy\b', r'\bwe\b', r'\bour\b', r'\bme\b']

ACTIVE_VERBS = [
    'led','managed','built','developed','designed','implemented',
    'delivered','coordinated','executed','achieved','improved',
    'reduced','increased','trained','supervised','operated',
    'maintained','conducted','performed','established',
]

STANDARD_FONTS = ['arial','calibri','times new roman','helvetica','garamond','cambria','georgia']


def score_resume(extracted: dict, parsed: dict, job_description: str = '') -> dict:
    format_score,  format_issues  = score_format(extracted, parsed)
    section_score, section_issues = score_sections(parsed, extracted.get('raw_text', ''))
    keyword_score, keyword_data   = score_keywords(extracted['raw_text'], job_description)

    total = format_score + section_score + keyword_score

    if total >= 85:   grade = 'EXCELLENT'
    elif total >= 75: grade = 'GOOD'
    elif total >= 60: grade = 'PASS'
    else:             grade = 'FAIL'

    return {
        'total_score': total,
        'grade':       grade,
        'breakdown':   {'format': format_score, 'sections': section_score, 'keywords': keyword_score},
        'issues':      format_issues + section_issues,
        'keyword_data': keyword_data,
    }


def score_format(extracted: dict, parsed: dict) -> tuple:
    score  = 40
    issues = []
    raw    = extracted.get('raw_text', '')
    structural = extracted.get('structural_issues', [])

    deductions = {
        'TABLES_DETECTED':                 8,
        'CONTENT_IN_HEADER':               6,
        'CONTENT_IN_FOOTER':               4,
        'IMAGES_OR_GRAPHICS_DETECTED':     5,
        'SCANNED_PDF_DETECTED_OCR_NEEDED': 15,
        'UNSUPPORTED_FILE_TYPE':           40,
        'MULTI_COLUMN_LAYOUT_DETECTED':    8,
    }
    for issue in structural:
        deduct = deductions.get(issue, 0)
        score -= deduct
        issues.append(f'FORMAT: {issue} (-{deduct}pts)')

    contact = parsed.get('contact', {})
    if not contact.get('email'):
        score -= 5
        issues.append('FORMAT: EMAIL_NOT_FOUND (-5pts) [R16]')
    if not contact.get('phone'):
        score -= 3
        issues.append('FORMAT: PHONE_NOT_FOUND (-3pts) [R16]')
    if not contact.get('linkedin'):
        score -= 2
        issues.append('FORMAT: LINKEDIN_URL_MISSING (-2pts) [R37]')

    bad_chars = ['★','➤','☑','■','◆','✓','●','→','✗']
    if any(c in raw for c in bad_chars):
        score -= 4
        issues.append('FORMAT: SPECIAL_UNICODE_SYMBOLS_DETECTED (-4pts) [R11]')

    dates_slash = re.findall(r'\d{2}/\d{4}', raw)
    dates_text  = re.findall(r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{4}', raw)
    if dates_slash and dates_text:
        score -= 3
        issues.append('FORMAT: INCONSISTENT_DATE_FORMAT (-3pts) [R24]')

    passive_found = [p for p in PASSIVE_PATTERNS if re.search(p, raw, re.IGNORECASE)]
    if passive_found:
        score -= 3
        issues.append('FORMAT: PASSIVE_VOICE_DETECTED (-3pts) [R35]')

    pronouns_found = [p for p in PERSONAL_PRONOUNS if re.search(p, raw)]
    if pronouns_found:
        score -= 3
        issues.append('FORMAT: PERSONAL_PRONOUNS_DETECTED (-3pts) [R36]')

    common_errors = {
        'manger': 'manager', 'recieved': 'received', 'acheived': 'achieved',
        'responsibilty': 'responsibility', 'managment': 'management',
        'experiance': 'experience', 'comunication': 'communication',
    }
    raw_lower = raw.lower()
    for wrong, correct in common_errors.items():
        if wrong in raw_lower:
            score -= 2
            issues.append(f'FORMAT: SPELLING_ERROR "{wrong}" → "{correct}" (-2pts) [R38]')

    has_numbers = bool(re.search(
        r'\d+\s*(%|years?|months?|vessels?|crew|GRT|DWT|million|thousand)',
        raw, re.IGNORECASE
    ))
    if not has_numbers:
        score -= 3
        issues.append('FORMAT: NO_QUANTIFIED_ACHIEVEMENTS (-3pts) [R32]')

    return max(score, 0), issues


def score_sections(parsed: dict, raw_text: str = '') -> tuple:
    """
    Detects sections from BOTH parsed['sections'] dict AND raw_text.
    Needed because the re-scorer reads a generated DOCX where headings
    are 'PROFESSIONAL SUMMARY' / 'WORK EXPERIENCE' — not just 'summary'.
    """
    score  = 0
    issues = []
    sections = parsed.get('sections', {})
    raw_lower = raw_text.lower()

    # Section detection: check parsed dict first, fall back to raw_text scan
    # raw_text patterns match what generator.py actually writes as headings
    section_checks = {
        'summary': {
            'label': 'SUMMARY_MISSING',
            'pts':   4,
            'raw_patterns': [
                'professional summary', 'summary', 'profile', 'objective', 'about me'
            ],
        },
        'experience': {
            'label': 'EXPERIENCE_MISSING',
            'pts':   6,
            'raw_patterns': [
                'work experience', 'experience', 'employment', 'sea service',
                'vessel experience', 'work history'
            ],
        },
        'education': {
            'label': 'EDUCATION_MISSING',
            'pts':   3,
            'raw_patterns': [
                'education', 'academic', 'qualification', 'training', 'pre-sea'
            ],
        },
        'skills': {
            'label': 'SKILLS_MISSING',
            'pts':   4,
            'raw_patterns': [
                'skills', 'competencies', 'technical skills', 'key skills'
            ],
        },
        'certifications': {
            'label': 'CERTIFICATIONS_MISSING',
            'pts':   3,
            'raw_patterns': [
                'certifications', 'certificates', 'certification', 'licence', 'license'
            ],
        },
    }

    for key, cfg in section_checks.items():
        # Check 1: parsed sections dict has content
        in_parsed = bool(sections.get(key, '').strip())
        # Check 2: raw_text contains the heading (catches generated DOCX)
        in_raw = any(pat in raw_lower for pat in cfg['raw_patterns'])

        if in_parsed or in_raw:
            score += cfg['pts']
        else:
            issues.append(f'SECTION: {cfg["label"]} (-{cfg["pts"]}pts) [R18]')

    # Summary quality — check raw_text if section not in parsed
    summary = sections.get('summary', '')
    if not summary and raw_lower:
        # Extract summary text from raw (between PROFESSIONAL SUMMARY and next heading)
        m = re.search(
            r'professional summary\s*\n(.+?)(?=\n[A-Z ]{4,}\n|\Z)',
            raw_text, re.IGNORECASE | re.DOTALL
        )
        if m:
            summary = m.group(1).strip()

    if summary:
        sentences = len(re.findall(r'[.!?]+', summary))
        if sentences < 3:
            issues.append('SECTION: SUMMARY_TOO_SHORT — aim for 3-5 sentences [R19]')
        if sentences > 6:
            issues.append('SECTION: SUMMARY_TOO_LONG — keep to 3-5 sentences [R19]')

    # Skills count
    skills = parsed.get('skills', [])
    if len(skills) < 10:
        issues.append(f'SECTION: LOW_SKILL_COUNT ({len(skills)} found, target 15-25) [R20]')
    elif len(skills) > 30:
        issues.append(f'SECTION: TOO_MANY_SKILLS ({len(skills)} found, max 25 recommended) [R20]')

    # Active verbs — check raw_text experience block
    experience = sections.get('experience', '')
    if not experience:
        # Try to find experience section in raw_text
        m = re.search(
            r'work experience\s*\n(.+?)(?=\nskills|\ncertif|\neducation|\Z)',
            raw_text, re.IGNORECASE | re.DOTALL
        )
        if m:
            experience = m.group(1)

    if experience:
        exp_lower = experience.lower()
        active_verb_count = sum(1 for v in ACTIVE_VERBS if v in exp_lower)
        if active_verb_count < 3:
            issues.append('SECTION: FEW_ACTIVE_VERBS_IN_EXPERIENCE [R31]')

        years = re.findall(r'\b(20\d{2})\b', experience)
        if years and len(years) >= 2:
            years_int = [int(y) for y in years]
            if years_int[0] < years_int[-1]:
                issues.append('SECTION: EXPERIENCE_NOT_REVERSE_CHRONOLOGICAL [R21]')

    # Certifications acronym format
    certs = sections.get('certifications', '')
    if not certs:
        m = re.search(r'certif\w+\s*\n(.+?)(?=\n[A-Z ]{4,}\n|\Z)', raw_text, re.IGNORECASE | re.DOTALL)
        if m:
            certs = m.group(1)
    if certs:
        has_acronym = bool(re.search(r'\(([A-Z]{2,})\)', certs))
        if not has_acronym:
            issues.append('SECTION: CERTS_MISSING_ACRONYM_FORMAT [R23]')

    return score, issues


def score_keywords(resume_text: str, job_description: str) -> tuple:
    if not job_description or not job_description.strip():
        return 20, {
            'match_pct': None,
            'missing_keywords': [],
            'note': 'No JD provided — base score assigned',
        }
    try:
        def preprocess(text):
            text = text.lower()
            text = re.sub(r'[^\w\s\-]', ' ', text)
            return re.sub(r'\s+', ' ', text).strip()

        JD_FILLER = {
            'the','and','for','are','with','this','that','have','from',
            'will','been','has','had','but','not','all','were','when',
            'can','required','minimum','essential','mandatory','knowledge',
            'experience','on','or','in','of','a','is','to','must','should',
            'strong','good','ability','skills','role','preferred','valid',
            'current','active','least','years','year','month','months',
        }

        jd_clean  = preprocess(job_description)
        res_clean = preprocess(resume_text)
        jd_words  = set(jd_clean.split()) - JD_FILLER
        res_words = set(res_clean.split())

        if not jd_words:
            return 20, {'note': 'No meaningful keywords in JD'}

        found     = jd_words & res_words
        missing   = sorted(jd_words - res_words)
        match_pct = round(len(found) / len(jd_words) * 100, 1)

        try:
            vectorizer = TfidfVectorizer(stop_words='english', ngram_range=(1, 2))
            vectors    = vectorizer.fit_transform([jd_clean, res_clean])
            cosine     = round(cosine_similarity(vectors[0], vectors[1])[0][0] * 100, 1)
        except Exception:
            cosine = 0.0

        blended_pct = (match_pct * 0.7) + (cosine * 0.3)
        keyword_pts = max(0, min(40, int((blended_pct / 100) * 40)))

        notes = []
        if match_pct < 65:
            notes.append(f'Low keyword match — consider adding: {", ".join(missing[:8])}')

        return keyword_pts, {
            'match_pct':        match_pct,
            'cosine_pct':       cosine,
            'missing_keywords': missing[:15],
            'found_keywords':   sorted(found),
            'note':             f'{match_pct}% of JD keywords found ({len(found)}/{len(jd_words)})',
            'extra_notes':      notes,
        }

    except Exception as e:
        return 20, {'error': str(e)}