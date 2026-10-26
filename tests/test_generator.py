# tests/test_generator.py
# Regression tests for pipeline/generator.py — sentence-splitting,
# length-limit enforcement, and (below) real document-building bugs
# found via actual generate_docx() runs against real candidate data:
# the certifications page-length cap silently dropping real certs, the
# missing Sea Service Record section, python-docx crashing on XML-
# illegal control characters, and photo embedding.

import os

from pipeline.generator import split_into_sentences


def test_split_into_sentences_normal_case():
    text = 'Second officer with 5 years experience. Operated ECDIS and radar systems. Compliant with COLREGS and ISM Code.'
    result = split_into_sentences(text)
    assert result == [
        'Second officer with 5 years experience',
        'Operated ECDIS and radar systems',
        'Compliant with COLREGS and ISM Code',
    ]


def test_split_into_sentences_preserves_vessel_abbreviations():
    """
    Regression test for a real bug found on generated output: naive
    split('.') treated 'M.T' and 'M.V' (standard maritime vessel-prefix
    abbreviations for Motor Tanker / Motor Vessel) as sentence
    boundaries, truncating summaries mid-word — e.g. a real generated
    resume ended with "...Sailed on M. T SG Pegasus GRT 8195, M." after
    the 3-sentence cap kicked in on what was actually only 2 sentences.
    """
    text = (
        'Sailed on M.T SG Pegasus GRT 8195, M.T Zeal Start GRT 27987, '
        'and M.V Ore Brasil GRT 199631 across a range of tanker and '
        'bulk carrier operations.'
    )
    result = split_into_sentences(text)
    assert len(result) == 1, f'Expected one sentence, got {result}'
    assert 'GRT 199631' in result[0], 'Lost content after the last M.V abbreviation'
    assert not result[0].endswith('M'), 'Truncated mid-abbreviation'


def test_split_into_sentences_handles_double_period_abbreviation():
    """
    Real bug fixed, confirmed on real generated output: the original fix
    only guarded ONE period per abbreviation (checking if the
    accumulated fragment was the single character "M"). "M.V." -- a
    period after EACH letter, as opposed to "M.T" with only one -- still
    tripped a false sentence boundary on its SECOND period, since by
    then the accumulated fragment was the two-character "word" "M.V",
    which the original len(last_word) == 1 check didn't match. Confirmed
    on a real summary truncated to "...on M.V. FEDERAL ST LAURENT and
    M.V." once the 3-sentence cap kicked in on a spurious 4th "sentence"
    boundary.
    """
    text = (
        'Managed maintenance on M.V. FEDERAL ST LAURENT and M.V. FEDERAL '
        'RIDEAU, ensuring operational efficiency across both vessels.'
    )
    result = split_into_sentences(text)
    assert len(result) == 1, f'Expected one sentence, got {result}'
    assert 'FEDERAL RIDEAU' in result[0], 'Lost content after the second M.V. abbreviation'
    assert not result[0].rstrip().endswith('M.V'), 'Truncated mid-abbreviation'


def test_split_into_sentences_does_not_confuse_acronyms_with_abbreviations():
    """A multi-letter acronym like STCW ending a sentence is NOT an abbreviation."""
    text = 'Holds valid certification STCW. Experienced in cargo operations.'
    result = split_into_sentences(text)
    assert result == ['Holds valid certification STCW', 'Experienced in cargo operations']


def test_split_into_sentences_empty_input():
    assert split_into_sentences('') == []
    assert split_into_sentences(None) == []


def test_enforce_length_limits_no_longer_truncates_mid_abbreviation():
    """
    End-to-end regression test at the enforce_length_limits() level,
    reproducing the exact real failure mode: a summary with only 2 real
    sentences containing vessel abbreviations must not be cut down to a
    truncated fragment by the 3-sentence cap.
    """
    from pipeline.generator import enforce_length_limits

    real_case = (
        'As a Third Officer with over 3 years of sea service experience on '
        'various vessel types including Chemical/Oil Products Tankers and '
        'Bulk Carriers, demonstrated ability to manage vessel operations was '
        'evident. Sailed on M.T SG Pegasus GRT 8195, M.T Zeal Start GRT 27987, '
        'and M.V Ore Brasil GRT 199631, gaining broad experience.'
    )
    content = {'summary': real_case, 'rank': 'third officer'}
    result = enforce_length_limits(content, 'third officer')
    assert not result['summary'].rstrip().endswith('M.'), 'Still truncating mid-abbreviation'
    assert 'GRT 199631' in result['summary'], 'Lost the final clause'


def test_enforce_length_limits_enforces_overall_word_budget_for_junior_rank():
    """
    Regression test: enforce_length_limits previously only capped
    per-field counts (skills count, bullets-per-entry) with no check on
    total rendered word count -- a junior-rank resume with the maximum
    allowed entries/bullets/skills, each individually long, could still
    overflow well past a single page. Confirms a deliberately oversized
    junior-rank resume gets trimmed down to the single-page word budget.
    """
    from pipeline.generator import enforce_length_limits, WORD_LIMIT_SINGLE_PAGE, _count_words

    # 60-word bullets: still over the 600-word budget even after the
    # existing per-field caps (skills->15, certs->8, entries->3,
    # bullets->3 each) run first -- those caps alone don't bound total
    # word count, only item counts, which is exactly the gap this test
    # targets -- while leaving enough margin that the word-budget floors
    # (skills>=10, certs>=4, bullets>=2/entry) can actually reach budget.
    long_bullet = ' '.join(['word'] * 60)
    content = {
        'rank': 'deck cadet',
        'summary': ' '.join(['word'] * 60) + '.',
        'skills': [f'Skill {i}' for i in range(20)],
        'certifications': [f'Certification Number {i} Full Name (ACR) | Issuer | 2024' for i in range(10)],
        'experience_bullets': [
            {'role': 'Deck Cadet', 'company': 'Example Shipping', 'dates': '01/2022 - 01/2023',
             'bullets': [long_bullet, long_bullet, long_bullet]},
            {'role': 'Deck Cadet', 'company': 'Second Shipping', 'dates': '01/2021 - 12/2021',
             'bullets': [long_bullet, long_bullet, long_bullet]},
            {'role': 'Deck Cadet', 'company': 'Third Shipping', 'dates': '01/2020 - 12/2020',
             'bullets': [long_bullet, long_bullet, long_bullet]},
        ],
    }

    result = enforce_length_limits(content, 'deck cadet')

    assert _count_words(result) <= WORD_LIMIT_SINGLE_PAGE, (
        f'Expected total word count <= {WORD_LIMIT_SINGLE_PAGE}, got {_count_words(result)}'
    )
    # Every experience ENTRY must survive -- only bullets-per-entry may shrink,
    # per Rule 2 ("Include ALL vessel entries from source — do not drop any").
    assert len(result['experience_bullets']) == 3
    for job in result['experience_bullets']:
        assert len(job['bullets']) >= 2, 'Bullets should never be trimmed below the 2-bullet floor'


def test_enforce_length_limits_leaves_already_short_content_untouched():
    """A resume already well within budget must not be trimmed at all by the word-budget pass."""
    from pipeline.generator import enforce_length_limits

    content = {
        'rank': 'third officer',
        'summary': 'A short summary.',
        'skills': ['STCW', 'ECDIS'],
        'certifications': ['CoC | DG Shipping | 2023'],
        'experience_bullets': [
            {'role': 'Third Officer', 'company': 'Example Shipping', 'dates': '01/2022 - 01/2023',
             'bullets': ['A short bullet.', 'Another short bullet.']},
        ],
    }
    result = enforce_length_limits(content, 'third officer')
    assert result['skills'] == ['STCW', 'ECDIS']
    assert result['certifications'] == ['CoC | DG Shipping | 2023']
    assert len(result['experience_bullets'][0]['bullets']) == 2


# ── Priority 0: certifications are never truncated for page-length ─────

def test_enforce_length_limits_never_truncates_certifications():
    """
    Real bug fixed: a hardcoded [:8] cap (plus a separate word-budget
    trim loop) used to silently drop real certifications for page-length
    reasons — confirmed on real generated output, where 2 of a real
    candidate's 10 real certs/courses were dropped even though the AI
    rewrite itself had preserved all 10. Certifications must survive
    enforce_length_limits regardless of count or total length; page
    budget is enforced via skills/bullets trimming and compact_mode
    (font/spacing) instead. Deliberately oversized (15 certs, well past
    the old 8-cap) to prove there's no cap left at all.
    """
    from pipeline.generator import enforce_length_limits

    certs = [f'Certification {i} (ACR{i}) | Issuer | 2024' for i in range(15)]
    content = {'rank': 'chief officer', 'certifications': list(certs)}
    result = enforce_length_limits(content, 'chief officer')
    assert result['certifications'] == certs, 'Certifications must never be dropped by length enforcement'


def test_enforce_length_limits_sets_compact_mode_when_still_over_budget():
    """
    With certifications no longer trimmable, a genuinely long cert list
    can still push total words over budget after skills/bullets are
    already at their floors — compact_mode is the last-resort lever
    (smaller fonts/tighter spacing in generate_docx) that keeps every
    fact intact instead of deleting content to fit.
    """
    from pipeline.generator import enforce_length_limits

    long_certs = [f'A Very Long Certification Name Number {i} Full Detail | Issuing Authority Name | 2024'
                  for i in range(100)]
    content = {'rank': 'chief officer', 'certifications': long_certs, 'summary': 'Short summary.'}
    result = enforce_length_limits(content, 'chief officer')
    assert result['_compact_mode'] is True
    assert len(result['certifications']) == 100, 'Compact mode must not have dropped any certs either'


# ── Priority 1.3: Sea Service Record section ────────────────────────────

def test_add_sea_service_section_renders_roles_reverse_chronological_order():
    from docx import Document
    from pipeline.generator import add_sea_service_section

    doc = Document()
    content = {
        'sea_months': 18,
        'vessel_types': ['chemical tanker', 'oil tanker'],
        'experience_bullets': [
            {'role': 'Second Officer', 'company': 'Newer Shipping Co', 'dates': '06/2023 - 12/2025',
             'bullets': ['Led navigation aboard M.T Example Star, GRT 62795, ensuring safe passage.']},
            {'role': 'Third Officer', 'company': 'Older Shipping Co', 'dates': '01/2020 - 05/2022',
             'bullets': ['Assisted watchkeeping duties.']},
        ],
    }
    add_sea_service_section(doc, content)
    text = '\n'.join(p.text for p in doc.paragraphs)

    assert 'SEA SERVICE RECORD' in text
    assert 'Total Sea Service: 18 months' in text
    assert 'Chemical Tanker' in text and 'Oil Tanker' in text

    # Row order follows input order (reverse-chronological, most recent
    # first) -- checked via company names rather than role titles, since
    # the first row's role is intentionally replaced by its vessel/GRT
    # detail (see below) to avoid duplicating Work Experience's header line.
    newer_idx = text.index('Newer Shipping Co')
    older_idx = text.index('Older Shipping Co')
    assert newer_idx < older_idx, 'Expected reverse-chronological order (most recent role first)'

    # First entry has extractable vessel/GRT -- its row leads with that,
    # not a restatement of "Second Officer — Newer Shipping Co | dates"
    # (Work Experience's own bold header shows that immediately below).
    assert 'M.T Example Star' in text and 'GRT 62795' in text, (
        'Expected best-effort vessel/GRT extraction from the bullet text'
    )
    assert 'Second Officer' not in text, (
        'Expected the role title to be replaced by the vessel/GRT detail, not duplicated'
    )
    # Real bug fixed, confirmed on real output: the source bullet's
    # sentence punctuation right after the number ("GRT 62795, ensuring
    # safe passage") used to get captured as part of the number itself,
    # rendering "(GRT 62795,)" with a dangling comma. A substring check
    # for "GRT 62795" alone doesn't catch this (it's still a substring
    # of "GRT 62795,"), so this checks the exact rendered detail instead.
    assert 'GRT 62795,)' not in text, f'Trailing comma leaked into GRT value: {text!r}'
    assert 'M.T Example Star (GRT 62795)' in text

    # Second entry has no extractable vessel/GRT in its bullet -- falls
    # back to the role title so the row isn't just bare dates/company.
    assert 'Third Officer' in text


def test_add_sea_service_section_omits_itself_when_no_experience():
    from docx import Document
    from pipeline.generator import add_sea_service_section

    doc = Document()
    add_sea_service_section(doc, {'experience_bullets': []})
    assert doc.paragraphs == []


def test_add_sea_service_section_does_not_crash_on_list_valued_fields():
    """
    Real crash fixed, confirmed on a real live model response: 'company'
    (or 'dates'/'role') came back as a list instead of a string --
    schema drift, the same category _stringify_cert already guards
    against for certifications. The duplicate-row check built a
    (dates, company, headline) tuple directly from these fields and used
    it as a set key, raising "TypeError: unhashable type: 'list'" and
    crashing DOCX generation entirely -- losing an otherwise-successful
    AI rewrite. _job_field must coerce any list value to a string before
    it's used as part of that key.
    """
    from docx import Document
    from pipeline.generator import add_sea_service_section

    doc = Document()
    content = {
        'experience_bullets': [
            {'role': 'Second Officer', 'company': ['MSC Crewing Services', 'MSC'],
             'dates': '01/2020 - 12/2021', 'bullets': ['Served aboard M.T Example, GRT 12345.']},
        ],
    }
    add_sea_service_section(doc, content)  # must not raise
    text = '\n'.join(p.text for p in doc.paragraphs)
    assert 'MSC Crewing Services' in text and 'MSC' in text


def test_add_sea_service_section_omits_dangling_separator_for_blank_company():
    """
    Real bug fixed, confirmed on a real live model response (Ankur
    Shrivastava): 'company' came back as an empty string for every role,
    and the old f-string unconditionally included it, rendering
    "2018-01-01 - 2020-12-31   —   Second Engineer   —   " with a
    trailing dash and nothing after it. Only the parts actually present
    should be joined.
    """
    from docx import Document
    from pipeline.generator import add_sea_service_section

    doc = Document()
    content = {
        'experience_bullets': [
            {'role': 'Second Engineer', 'company': '', 'dates': '01/2018 - 12/2020',
             'bullets': ['Managed engine room operations.']},
        ],
    }
    add_sea_service_section(doc, content)
    text = '\n'.join(p.text for p in doc.paragraphs)
    assert '01/2018 - 12/2020   —   Second Engineer' in text
    assert text.rstrip().endswith('Second Engineer'), f'Expected no dangling separator, got: {text!r}'
    assert '—   \n' not in text and not text.rstrip().endswith('—')


def test_generate_docx_work_experience_omits_dangling_separator_for_blank_company(tmp_path, monkeypatch):
    """Same real bug as above, in the Work Experience section's bold role_line."""
    import config
    from pipeline.generator import generate_docx
    monkeypatch.setattr(config, 'OUTPUT_DIR', str(tmp_path))

    content = {
        'name': 'Ankur Shrivastava', 'contact': {'email': 'a@b.com', 'phone': '123'},
        'summary': 'Second Engineer with extensive tanker experience.',
        'experience_bullets': [
            {'role': 'Second Engineer', 'company': '', 'dates': '01/2018 - 12/2020',
             'bullets': ['Managed engine room operations.']},
        ],
        'skills': ['STCW'] * 15, 'certifications': ['STCW Certificate'],
        'education': ['B.Tech — University — 2004'], 'rank': 'second engineer',
    }
    path = generate_docx(content, job_id='test_blank_company')
    from docx import Document
    text = '\n'.join(p.text for p in Document(path).paragraphs)
    assert 'Second Engineer  |  01/2018 - 12/2020' in text, f'Expected clean role/dates line, got: {text!r}'
    assert '—    |' not in text, f'Expected no dangling separator before the pipe, got: {text!r}'


# ── XML-illegal control characters (crash regression) ───────────────────

def test_generate_docx_does_not_crash_on_control_characters(tmp_path, monkeypatch):
    """
    Real crash fixed: a certification line pulled verbatim from a garbled
    PDF extraction (services/ai_client.py's enforce_cert_completeness
    safety net appends original source lines) can contain a stray XML-
    illegal control character. python-docx/lxml used to raise ValueError
    ("All strings must be XML compatible") and crash the whole DOCX
    generation — confirmed on real output. generate_docx() must now
    strip these and complete successfully.
    """
    import config
    from pipeline.generator import generate_docx

    monkeypatch.setattr(config, 'OUTPUT_DIR', str(tmp_path))

    content = {
        'rank': 'third officer',
        'name': 'Test Candidate',
        'contact': {'email': 'test@example.com'},
        'summary': 'A clean summary sentence. Second sentence. Third sentence.',
        'certifications': ['STCW (STCW) | DG Shipping | 2023\x0b\x0cTrailing control chars'],
        'skills': ['STCW'],
        'experience_bullets': [
            {'role': 'Third Officer', 'company': 'Example Co', 'dates': '01/2022 - 01/2023',
             'bullets': ['Did work.\x01 with a stray control byte']},
        ],
    }
    path = generate_docx(content, job_id='control-char-test')
    assert os.path.exists(path)

    from docx import Document
    doc = Document(path)
    full_text = '\n'.join(p.text for p in doc.paragraphs)
    assert '\x0b' not in full_text and '\x0c' not in full_text and '\x01' not in full_text
    assert 'Trailing control chars' in full_text, 'Expected the surrounding real text to survive, only the control byte stripped'


def test_generated_docx_is_owner_readable_only(tmp_path, monkeypatch):
    import stat
    import pipeline.generator as generator

    monkeypatch.setattr(generator, 'OUTPUT_DIR', str(tmp_path))
    path = generator.generate_docx({'name': 'Test Candidate'}, job_id='private-output')
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


# ── Priority 2: photo embedding ──────────────────────────────────────────

def _tiny_jpeg_bytes() -> bytes:
    """A minimal real, valid JPEG — synthesized, not a real candidate photo (no PII in the test suite)."""
    import io
    from PIL import Image as PILImage
    img = PILImage.new('RGB', (100, 100), color=(120, 140, 160))
    buf = io.BytesIO()
    img.save(buf, format='JPEG')
    return buf.getvalue()


def test_generate_docx_with_photo_embeds_a_real_inline_image(tmp_path, monkeypatch):
    import config
    from pipeline.generator import generate_docx

    monkeypatch.setattr(config, 'OUTPUT_DIR', str(tmp_path))

    content = {
        'rank': 'eto', 'name': 'Test Candidate', 'contact': {'email': 'test@example.com'},
        'summary': 'A summary. Second sentence. Third sentence.',
        'certifications': [], 'skills': ['STCW'], 'experience_bullets': [],
    }
    path = generate_docx(content, job_id='photo-test', include_photo=True, photo_bytes=_tiny_jpeg_bytes())
    assert path.endswith('_photo.docx')

    from docx import Document
    doc = Document(path)
    assert len(doc.inline_shapes) == 1, 'Expected exactly one real inline image in the document body'


def test_generate_docx_without_photo_has_no_media():
    from pipeline.generator import generate_docx

    content = {
        'rank': 'eto', 'name': 'Test Candidate', 'contact': {'email': 'test@example.com'},
        'summary': 'A summary. Second sentence. Third sentence.',
        'certifications': [], 'skills': ['STCW'], 'experience_bullets': [],
    }
    path = generate_docx(content, job_id='no-photo-test')
    assert not path.endswith('_photo.docx')

    from docx import Document
    doc = Document(path)
    assert len(doc.inline_shapes) == 0


def test_generate_docx_renders_rank_under_the_candidate_name(tmp_path, monkeypatch):
    """
    Real, cross-candidate bug fixed: content.get('rank') was never
    referenced anywhere in this file at all -- confirmed on real
    generated output for two independent candidates (Abdullah Tauqir
    Khan, Aashish Kumar) that the document went straight from the
    candidate's name to their contact info with no title/rank line
    between them, regardless of the AI model used. Distinct from the
    earlier fix for a blank job['role'] inside a Work Experience entry
    (that one patches a rank the model failed to fill into an EXISTING
    rendered field; this is a field that was never rendered at all).
    """
    import config
    from pipeline.generator import generate_docx
    from docx import Document

    monkeypatch.setattr(config, 'OUTPUT_DIR', str(tmp_path))

    content = {
        'rank': 'third officer', 'name': 'Test Candidate', 'contact': {'email': 'test@example.com'},
        'summary': 'A summary. Second sentence. Third sentence.',
        'certifications': [], 'skills': ['STCW'], 'experience_bullets': [],
    }
    path = generate_docx(content, job_id='rank-header-test')
    doc = Document(path)
    paragraph_texts = [p.text for p in doc.paragraphs if p.text.strip()]

    assert 'Test Candidate' in paragraph_texts[0]
    assert paragraph_texts[1] == 'Third Officer', (
        f'Expected the rank to render as its own paragraph directly under the name, got: {paragraph_texts[:3]}'
    )


def test_generate_docx_omits_rank_line_when_rank_is_empty(tmp_path, monkeypatch):
    """No rank -> no empty/awkward blank line where the title would go."""
    import config
    from pipeline.generator import generate_docx
    from docx import Document

    monkeypatch.setattr(config, 'OUTPUT_DIR', str(tmp_path))

    content = {
        'rank': '', 'name': 'Test Candidate', 'contact': {'email': 'test@example.com'},
        'summary': 'A summary. Second sentence. Third sentence.',
        'certifications': [], 'skills': ['STCW'], 'experience_bullets': [],
    }
    path = generate_docx(content, job_id='no-rank-test')
    doc = Document(path)
    paragraph_texts = [p.text for p in doc.paragraphs if p.text.strip()]

    assert paragraph_texts[1] != '', 'Expected no blank rank paragraph when rank is empty'
    assert 'email' in paragraph_texts[1] or '@' in paragraph_texts[1], (
        f'Expected contact info directly after the name when rank is empty, got: {paragraph_texts[:3]}'
    )


def test_generate_docx_gracefully_skips_photo_when_bytes_are_none(tmp_path, monkeypatch):
    """include_photo=True with no actual bytes must not crash or produce a broken image — just no photo."""
    import config
    from pipeline.generator import generate_docx

    monkeypatch.setattr(config, 'OUTPUT_DIR', str(tmp_path))

    content = {
        'rank': 'eto', 'name': 'Test Candidate', 'contact': {'email': 'test@example.com'},
        'summary': 'A summary. Second sentence. Third sentence.',
        'certifications': [], 'skills': ['STCW'], 'experience_bullets': [],
    }
    path = generate_docx(content, job_id='no-bytes-test', include_photo=True, photo_bytes=None)
    from docx import Document
    doc = Document(path)
    assert len(doc.inline_shapes) == 0


# ── Compact mode font scaling ────────────────────────────────────────────

def test_compact_mode_reduces_body_font_size(tmp_path, monkeypatch):
    import config
    from pipeline.generator import generate_docx

    monkeypatch.setattr(config, 'OUTPUT_DIR', str(tmp_path))

    long_certs = [f'A Very Long Certification Name Number {i} Full Detail | Issuing Authority Name | 2024'
                  for i in range(100)]
    content = {
        'rank': 'chief officer', 'name': 'Test Candidate', 'contact': {'email': 'test@example.com'},
        'summary': 'A summary. Second sentence. Third sentence.',
        'certifications': long_certs, 'skills': ['STCW'], 'experience_bullets': [],
    }
    path = generate_docx(content, job_id='compact-test')

    from docx import Document
    from docx.shared import Pt
    doc = Document(path)
    cert_para = next(p for p in doc.paragraphs if p.text.startswith('A Very Long Certification Name Number 0'))
    assert cert_para.runs[0].font.size == Pt(10), 'Expected compact mode to render certs at 10pt, not the normal 11pt'
