# tests/test_generator.py
# Regression tests for pipeline/generator.py's sentence-splitting fix.
# Not full generator.py coverage (that needs python-docx to exercise
# document-building) — this covers split_into_sentences(), a pure
# function, and the specific real bug it fixes.

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
