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
