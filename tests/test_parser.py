# tests/test_parser.py
# Regression coverage for pipeline/parser.py's section-boundary
# detection — the real, general bug found this session where common
# resume sections with no matching SECTION_HEADERS entry silently bled
# their content into whichever real section preceded them.

from pipeline.parser import split_sections


def test_split_sections_does_not_bleed_strengths_into_certifications():
    """
    Real, confirmed bug: 'Strengths' had no SECTION_HEADERS entry at
    all, so split_sections() (which only tracks RECOGNIZED headings)
    silently appended its content to whatever section came before it on
    the page. Confirmed on a real resume: this put "Management Skills",
    "Communication Skills", "Team Player", "Quick Learner" (a Strengths
    list) into the candidate's own Certifications section.
    """
    text = (
        'CERTIFICATIONS\n'
        'STCW Basic Safety Training\n'
        'Certificate of Competency (CoC)\n'
        'STRENGTHS\n'
        'Management Skills\n'
        'Communication Skills\n'
    )
    sections = split_sections(text)
    assert 'Management Skills' not in sections['certifications']
    assert 'Communication Skills' not in sections['certifications']
    assert 'STCW Basic Safety Training' in sections['certifications']


def test_split_sections_does_not_bleed_hobbies_and_references_into_languages():
    """
    Real, confirmed bug, same class as the Strengths one above: a
    referee's name and phone number ended up inside a candidate's own
    Languages section in real generated output, because 'Hobbies' and
    'References' had no SECTION_HEADERS entry either.
    """
    text = (
        'LANGUAGES\n'
        'English\n'
        'Arabic\n'
        'HOBBIES\n'
        'Travelling\n'
        'Reading\n'
        'REFERENCES\n'
        'Ajay Vazarkar | Chief Engineer\n'
        '91 9423777937\n'
    )
    sections = split_sections(text)
    assert sections['languages'].strip() == 'English\nArabic'
    assert 'Travelling' not in sections['languages']
    assert 'Ajay Vazarkar' not in sections['languages']
    assert 'Ajay Vazarkar' in sections['other']
    assert 'Travelling' in sections['other']
