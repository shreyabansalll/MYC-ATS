# tests/test_extractor.py
# Regression tests for pipeline/extractor.py's gutter-detection fix — a
# real word-corruption bug in the fixed-ratio splitter (_words_to_text
# called with a guessed split ratio): a paragraph line that word-wraps
# close to a *guessed* boundary had its trailing word(s) misclassified
# into the other column's bucket, silently relocating them out of the
# sentence (confirmed on a real generated resume). _detect_gutter_split()
# finds the split point from an actual empty gap in the page's word
# content instead of guessing, so a paragraph that belongs to one column
# can't have a word crossing it.
#
# These tests build synthetic pdfplumber-style word dicts
# ({'text', 'x0', 'x1', 'top'}) directly, so they don't require
# pdfplumber itself — only pipeline/extractor.py's pure helper functions
# are under test here.

from pipeline.extractor import _detect_gutter_split, _words_to_text


def _word(text, x0, x1, top):
    return {'text': text, 'x0': x0, 'x1': x1, 'top': top}


def test_detect_gutter_split_finds_real_gutter_between_columns():
    """
    Left column words end by x=170; right column (Courses sidebar) starts
    at x=400 — a genuine 230pt empty gutter. Page is 0-700 wide.
    """
    words = [
        _word('Summary', 40, 120, 40),
        _word('text', 130, 170, 40),
        _word('Courses', 400, 460, 40),
        _word('list', 465, 490, 40),
    ]
    split = _detect_gutter_split(words, x0=0, x1=700)
    assert 170 <= split <= 400, f'Expected split inside the real gutter, got {split}'


def test_detect_gutter_split_ignores_narrow_word_spacing():
    """Ordinary word-to-word spacing (a few points) must not be mistaken for a gutter."""
    words = [
        _word('A', 40, 55, 60), _word('motivated', 60, 140, 60),
        _word('Electrical', 145, 220, 60), _word('Cadet,', 225, 280, 60),
    ]
    split = _detect_gutter_split(words, x0=0, x1=700, min_gap=15.0)
    # No real gutter present -> falls back to page midpoint.
    assert split == 350.0


def test_detect_gutter_split_handles_wide_wrapped_paragraph():
    """
    Regression test for the real bug: a paragraph's wrapped lines extend
    to x=505 in the left column, well past where a guessed 0.45-0.60
    ratio (315-420 on a 700-wide page) would fall. The true gutter (words
    resume again at x=600 for the sidebar) must still be detected past
    the paragraph's own extent, not at a guessed ratio that cuts through it.
    """
    words = [
        _word('with', 40, 75, 60), _word('a', 80, 90, 60),
        _word('solid', 95, 135, 60), _word('foundation', 140, 230, 60),
        _word('in', 235, 250, 60), _word('electrical', 255, 340, 60),
        _word('engineering,', 345, 505, 60),
        _word('Courses', 600, 660, 60),
    ]
    split = _detect_gutter_split(words, x0=0, x1=700)
    assert split > 505, f'Detected split must fall past the paragraph, got {split}'
    assert split < 600, f'Detected split must fall before the sidebar, got {split}'

    # Feeding the detected split into the existing _words_to_text() must
    # keep the whole paragraph line intact, not fragment it.
    left_text = _words_to_text(words, 0, split)
    assert 'foundation in electrical engineering' in left_text
    assert 'Courses' not in left_text


def test_detect_gutter_split_empty_input():
    assert _detect_gutter_split([], x0=0, x1=700) == 350.0


def test_detect_gutter_split_handles_narrow_sidebar_layout():
    """
    Regression test: an 87%/13% wide-left/narrow-right template (common —
    a slim icon/category sidebar) has its gutter close to the page edge.
    An earlier version of this function restricted candidate gutters to
    25%-80% of page width, which excluded gutters like this one and
    silently fell back to the page midpoint instead of the real boundary.
    """
    words = [
        _word('Summary', 40, 120, 40),
        _word('paragraph', 130, 640, 40),
        _word('Courses', 685, 745, 40),
        _word('list', 690, 750, 60),
    ]
    split = _detect_gutter_split(words, x0=0, x1=750)
    assert 640 < split < 685, f'Expected split inside the real (narrow-sidebar) gutter, got {split}'


def test_words_to_text_does_not_fragment_a_line_on_cumulative_y_drift():
    """
    Regression test for a second, distinct bug found on the real Mrinal
    Thapa PDF: _words_to_text()'s line-grouping compared every word to the
    FIRST word of the current line, not the immediately preceding word.
    On a long line, small cumulative y drift between words (justified
    text, mixed bold/regular font metrics) can push a later word's 'top'
    more than y_tolerance away from the line's first word even though each
    word is within tolerance of its immediate neighbor -- incorrectly
    splitting one physical line into several fragments mid-sentence.

    Each word here drifts by 1.5pt from its immediate neighbor (well
    within y_tolerance=3.0), but the last word drifts 6pt from the first
    word -- enough to trigger the old anchor-to-first-word bug.
    """
    words = [
        _word('with', 40, 75, 100.0), _word('a', 80, 90, 101.5),
        _word('solid', 95, 135, 103.0), _word('foundation', 140, 230, 104.5),
        _word('in', 235, 250, 106.0), _word('electrical', 255, 340, 107.5),
    ]
    text = _words_to_text(words, x_min=0, x_max=700)
    assert text == 'with a solid foundation in electrical', (
        f'Line was fragmented by cumulative drift, got: {text!r}'
    )


def test_words_to_text_still_splits_on_a_genuine_new_line():
    """A real new line (large y jump) must still be split correctly after the drift fix."""
    words = [
        _word('First', 40, 90, 100.0), _word('line.', 95, 130, 100.0),
        _word('Second', 40, 100, 114.0), _word('line.', 105, 140, 114.0),
    ]
    text = _words_to_text(words, x_min=0, x_max=700)
    assert text == 'First line.\nSecond line.'
