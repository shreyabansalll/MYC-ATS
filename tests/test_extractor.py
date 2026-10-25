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

import os

from pipeline.extractor import (
    _detect_gutter_split, _words_to_text, _clean_text,
    _split_glued_words, _column_bleed_penalty,
    extract_pdf, _table_has_real_gridlines, _render_table_as_text,
)


def _word(text, x0, x1, top):
    return {'text': text, 'x0': x0, 'x1': x1, 'top': top}


def _load_real_words(fixture_name):
    """
    Loads a tab-separated (top, x0, x1, text) word dump captured from a
    real resume PDF via pdfplumber's page.extract_words(), for tests that
    need real ground-truth coordinates rather than synthetic ones.
    """
    path = os.path.join(os.path.dirname(__file__), 'fixtures', fixture_name)
    words = []
    with open(path) as f:
        for line in f:
            top, x0, x1, text = line.rstrip('\n').split('\t')
            words.append(_word(text, float(x0), float(x1), float(top)))
    return words


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


def test_full_real_page_reconstructs_summary_and_courses_correctly():
    """
    End-to-end integration test against the complete, real 411-word
    page-1 coordinate dump from Mrinal Thapa's actual resume PDF (see
    tests/fixtures/mrinal_thapa_page1_words.tsv) — not a hand-built
    synthetic scenario. Exercises the real _detect_gutter_split() +
    _words_to_text() pipeline exactly as extract_pdf() calls it.

    Before the header-row fix: the DOB on the header row collapsed the
    real gutter, the detected split fell back to the page midpoint
    (306.0), and every left-column word ending at the true column edge
    (x1=337.3) — "in", "and", "system", "hands-", "to", "industry", etc.
    — leaked into the Courses section as scattered fragments.
    """
    words = _load_real_words('mrinal_thapa_page1_words.tsv')
    x0_page, x1_page = 0.0, 612.0

    split = _detect_gutter_split(words, x0_page, x1_page)
    assert 337.3 < split < 361.4, f'Expected split inside the real gutter, got {split}'

    left_text = _clean_text(_words_to_text(words, x0_page, split))
    right_text = _clean_text(_words_to_text(words, split, x1_page + 1))

    # The full Summary paragraph must survive intact, no dropped/relocated words.
    flat_summary = left_text.replace('\n', ' ')
    assert 'with a solid foundation in electrical engineering' in flat_summary
    assert 'power distribution, and troubleshooting' in flat_summary
    assert 'engineering tools for system design' in flat_summary
    assert 'strong problem-solving abilities' in flat_summary
    assert 'expertise through hands- on training' in flat_summary or \
           'expertise through hands-on training' in flat_summary
    assert 'adhering to industry standards' in flat_summary

    # None of those words leaked into the right column.
    for stray in ['foundation in\n', 'and\ntroubleshoot']:
        assert stray not in right_text
    assert 'COURSES' in right_text
    assert 'SUMMARY' not in right_text

    # Courses entries reconstruct as coherent blocks, not scattered garbage.
    assert 'Advanced Fire Fighting' in right_text
    assert 'TOLANI MARITIME INSTITUTE' in right_text
    assert '20100566212400120' in right_text


# ── Priority 2: extract_photo() — built on synthetic PDFs, not a real
#    candidate file, so no PII in the test suite and no dependency on
#    the gitignored data/ directory ──────────────────────────────────────

def _tiny_jpeg_bytes() -> bytes:
    import io
    from PIL import Image as PILImage
    img = PILImage.new('RGB', (100, 100), color=(120, 140, 160))
    buf = io.BytesIO()
    img.save(buf, format='JPEG')
    return buf.getvalue()


def test_extract_photo_finds_headshot_sized_image_in_top_region(tmp_path):
    import io
    import pymupdf
    from PIL import Image as PILImage
    from pipeline.extractor import extract_photo

    pdf_path = tmp_path / 'synthetic.pdf'
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)  # US Letter, points
    # Plausible headshot: ~80x80pt, top-right — mirrors the real bounding
    # box confirmed on a real candidate PDF (x0=486 x1=565 top=27 bottom=107).
    page.insert_image(pymupdf.Rect(500, 30, 580, 110), stream=_tiny_jpeg_bytes())
    doc.save(str(pdf_path))
    doc.close()

    result = extract_photo(str(pdf_path))
    assert result is not None

    # Confirms the result is genuinely re-encoded (not just passed
    # through) and is python-docx-compatible.
    with PILImage.open(io.BytesIO(result)) as im:
        assert im.format == 'JPEG'


def test_extract_photo_ignores_wide_banner_image(tmp_path):
    """A full-width banner/logo (not headshot-shaped) must not be mistaken for a candidate photo."""
    import pymupdf
    from pipeline.extractor import extract_photo

    pdf_path = tmp_path / 'synthetic_banner.pdf'
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_image(pymupdf.Rect(20, 20, 590, 60), stream=_tiny_jpeg_bytes())  # wide, short banner
    doc.save(str(pdf_path))
    doc.close()

    assert extract_photo(str(pdf_path)) is None


def test_extract_photo_ignores_image_outside_top_third(tmp_path):
    """A headshot-sized image positioned mid-page (e.g. a stray icon) isn't the candidate photo."""
    import pymupdf
    from pipeline.extractor import extract_photo

    pdf_path = tmp_path / 'synthetic_midpage.pdf'
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_image(pymupdf.Rect(500, 400, 580, 480), stream=_tiny_jpeg_bytes())  # same size, mid-page
    doc.save(str(pdf_path))
    doc.close()

    assert extract_photo(str(pdf_path)) is None


def test_extract_photo_handles_no_photo_docx_and_missing_file_gracefully():
    from pipeline.extractor import extract_photo

    assert extract_photo('some_resume.docx') is None
    assert extract_photo('/nonexistent/path.pdf') is None
    assert extract_photo('') is None
    assert extract_photo(None) is None


# ── _split_glued_words: PDF-authoring artifact where two unrelated
#    table fields have zero character-gap between them ─────────────────

def test_split_glued_words_separates_letters_from_long_digit_run():
    """
    Real bug fixed, confirmed on a real resume's Courses table: an
    institute name wrapping to a second line ending in "Training" sat
    immediately against that course's reference number with literally no
    space character in the PDF's own content stream, so pdfplumber's
    own word-boundary detection (which only ever splits on a horizontal
    character gap) returned it as one token: "Training10502363112502312".
    """
    words = [_word('Training10502363112502312', 361.4, 465.5, 174.4)]
    result = _split_glued_words(words)
    assert [w['text'] for w in result] == ['Training', '10502363112502312']
    # x-span split proportionally and still ordered left-to-right.
    assert result[0]['x0'] == 361.4
    assert result[0]['x1'] == result[1]['x0']
    assert result[1]['x1'] == 465.5


def test_split_glued_words_leaves_normal_words_untouched():
    words = [_word('Certificate', 100.0, 150.0, 50.0), _word('of', 152.0, 160.0, 50.0)]
    result = _split_glued_words(words)
    assert [w['text'] for w in result] == ['Certificate', 'of']


def test_split_glued_words_does_not_split_a_real_short_word_with_digits():
    """A genuine short alphanumeric token (e.g. a rank abbreviation) must not be mistaken for the glued-artifact shape."""
    words = [_word('M4', 100.0, 110.0, 50.0), _word('GRT62795', 200.0, 230.0, 50.0)]
    result = _split_glued_words(words)
    # 'M4' has too few trailing digits and too few leading letters to
    # match the 10+-digit-run shape; 'GRT62795' only has 5 digits, below
    # the 10-digit threshold reference numbers in this domain use.
    assert [w['text'] for w in result] == ['M4', 'GRT62795']


# ── _column_bleed_penalty: distinguishes a cleanly-separated column
#    reconstruction from one with stray words swept in from the other
#    column ─────────────────────────────────────────────────────────────

def test_column_bleed_penalty_zero_for_clean_multiword_lines():
    clean = 'COURSES\nCertificate of Proficiency in Advanced Fire Fighting\nCentre For Maritime Training'
    assert _column_bleed_penalty(clean) == 0


def test_column_bleed_penalty_counts_isolated_stray_words():
    """
    Real bug fixed: confirmed on a real resume that the previous
    candidate-selection scoring (heading-keyword count + combined text
    length) couldn't distinguish a clean column split from a badly-bled
    one, since reassigning a word between columns changes neither score.
    This penalty is the actual signal -- a word swept in from the wrong
    column lands alone on its own short line, since its real line-
    neighbors stayed behind in the other column.
    """
    bled = 'COURSES\nin Certificate of Proficiency in 06/2025\nsystem\nhands-\nindustry'
    assert _column_bleed_penalty(bled) >= 2


def test_column_bleed_penalty_does_not_flag_headings_or_acronym_lines():
    text = 'COURSES\nSTCW\nCDC'
    assert _column_bleed_penalty(text) == 0


# ── extract_pdf(): exception handling for malformed/unreadable input ────
# Confirmed real gaps found in a deliberate audit (not accidental): fed
# the real pipeline a corrupt/truncated PDF, a password-protected PDF, a
# zero-byte file with a .pdf extension, and a garbage-content file. All
# were already caught (no unhandled crash), but the password-protected
# case produced an uninformative blank message ('PDF_READ_ERROR: ', empty
# after the colon -- pdfminer's PDFPasswordIncorrect carries no message)
# instead of a clear, actionable signal.

def test_extract_pdf_handles_corrupt_truncated_file_without_crashing(tmp_path):
    path = tmp_path / 'corrupt.pdf'
    path.write_bytes(b'%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< /Type /Catalog')
    result = extract_pdf(str(path))
    assert result['raw_text'] == ''
    assert any('PDF_READ_ERROR' in issue for issue in result['structural_issues'])


def test_extract_pdf_handles_zero_byte_file_without_crashing(tmp_path):
    path = tmp_path / 'empty.pdf'
    path.write_bytes(b'')
    result = extract_pdf(str(path))
    assert result['raw_text'] == ''
    assert any('PDF_READ_ERROR' in issue for issue in result['structural_issues'])


def test_extract_pdf_handles_garbage_content_without_crashing(tmp_path):
    path = tmp_path / 'garbage.pdf'
    path.write_bytes(b'%PDF-1.4\nTHIS IS NOT REAL PDF CONTENT, JUST GARBAGE BYTES.')
    result = extract_pdf(str(path))
    assert result['raw_text'] == ''
    assert any('PDF_READ_ERROR' in issue for issue in result['structural_issues'])


def test_extract_pdf_handles_a_circular_page_tree_reference_without_hanging(tmp_path):
    """
    Deliberate malformed-object-structure audit item: a /Pages node whose
    own /Parent points back to itself (and whose /Kids includes itself)
    is a classic pattern that can infinite-loop a naive recursive
    page-tree walker. Confirmed here it doesn't hang or crash the whole
    process -- pdfminer's own recursion guard raises a real
    RecursionError, which the existing broad except in extract_pdf
    already catches cleanly (same PDF_READ_ERROR path as any other
    unreadable file), fast (well under a second) rather than exhausting
    the call stack silently or hanging.
    """
    objs = [
        b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Parent 2 0 R /Kids [2 0 R 3 0 R] /Count 1 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << >> >>',
        b'<< /Length 44 >>\nstream\nBT /F1 12 Tf 72 720 Td (Circular test) Tj ET\nendstream',
    ]
    out = bytearray(b'%PDF-1.4\n')
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f'{i} 0 obj\n'.encode() + body + b'\nendobj\n'
    xref_offset = len(out)
    n = len(objs) + 1
    out += f'xref\n0 {n}\n'.encode() + b'0000000000 65535 f \n'
    for off in offsets:
        out += f'{off:010d} 00000 n \n'.encode()
    out += f'trailer\n<< /Size {n} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF'.encode()

    path = tmp_path / 'circular_page_tree.pdf'
    path.write_bytes(bytes(out))

    import time
    t0 = time.monotonic()
    result = extract_pdf(str(path))
    elapsed = time.monotonic() - t0

    assert elapsed < 5, f'Expected this to fail fast, took {elapsed:.2f}s'
    assert result['raw_text'] == ''
    assert any('PDF_READ_ERROR' in issue for issue in result['structural_issues'])


def test_extract_pdf_gives_a_clear_signal_for_a_password_protected_file(tmp_path):
    """
    Real gap fixed: the generic exception handler produced the
    uninformative 'PDF_READ_ERROR: ' (blank after the colon) for a real
    password-protected PDF, since pdfminer's PDFPasswordIncorrect carries
    no message at all. A dedicated PASSWORD_PROTECTED_PDF signal is
    specific and actionable (remove the password and re-upload) the same
    way SCANNED_PDF_DETECTED_OCR_NEEDED already is for a different
    unreadable-input shape.
    """
    import pymupdf

    path = tmp_path / 'password_protected.pdf'
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), 'Password protected test content.')
    doc.save(
        str(path),
        encryption=pymupdf.PDF_ENCRYPT_AES_256,
        user_pw='secret123',
        owner_pw='ownersecret456',
    )
    doc.close()

    result = extract_pdf(str(path))
    assert result['raw_text'] == ''
    assert result['structural_issues'] == ['PASSWORD_PROTECTED_PDF']


# ── Real gridded-table extraction (extract_tables() as primary path) ────

def test_table_has_real_gridlines_rejects_spurious_heuristic_detection():
    """
    Real bug guarded against, confirmed on a real resume (Ankur
    Shrivastava): pdfplumber's find_tables() detected a spurious 2-row,
    1-col "table" spanning almost an entire Summary/Technical-Expertise
    page purely from rect-based heuristics, despite the page having zero
    real ruling lines. A genuine gridded table (confirmed via direct
    inspection on real Seafarer's Service Record tables: 46-66 real
    ruling lines, 8 real columns) must have both real page.lines AND
    enough rows/columns to be trusted.
    """
    class _FakeCell:
        pass

    class _FakeRow:
        def __init__(self, n_cells):
            self.cells = [_FakeCell()] * n_cells

    class _FakeTable:
        def __init__(self, n_rows, n_cells):
            self.rows = [_FakeRow(n_cells) for _ in range(n_rows)]

    class _FakePageNoLines:
        lines = []

    class _FakePageWithLines:
        lines = [{'x0': 0, 'top': 0, 'x1': 10, 'bottom': 0}] * 10

    # No real ruling lines on the page at all -- reject regardless of shape.
    assert _table_has_real_gridlines(_FakePageNoLines(), _FakeTable(13, 8)) is False

    # Real lines present, but too small a shape (the real spurious case: 2 rows, 1 col).
    assert _table_has_real_gridlines(_FakePageWithLines(), _FakeTable(2, 1)) is False

    # Real lines present AND a real multi-row, multi-column shape -- trusted.
    assert _table_has_real_gridlines(_FakePageWithLines(), _FakeTable(13, 8)) is True


def test_render_table_as_text_produces_clean_pipe_delimited_rows():
    """
    Real bug fixed, confirmed on a real resume (Gilfred Chettiar): the
    word-coordinate reconstruction lost the "Company Name" and "Vessel
    Name" columns entirely from a real Seafarer's Service Record table
    (multi-line cell wrapping, e.g. "Bulk\\nCarriers", broke the
    row-grouping heuristic it relies on). extract_tables() pairs every
    cell correctly regardless of wrapping, since it reads the table's
    actual ruling-line grid rather than inferring rows from word
    y-coordinates -- this renders that structured data as clean,
    unambiguous text.
    """
    table_data = [
        ['Company Name', 'Vessel Name', 'Type of\nvessel', 'GRT', 'Rank', 'Sign On', 'Sign Off'],
        ['AG Maritime\nPvt Ltd', 'M.V. Hampton\nOcean', 'Bulk\nCarriers', '43291', 'Second\nOfficer', '01/03/2026', '18/03/2026'],
    ]
    rendered = _render_table_as_text(table_data)
    lines = rendered.split('\n')
    assert lines[0] == 'Company Name | Vessel Name | Type of vessel | GRT | Rank | Sign On | Sign Off'
    assert lines[1] == 'AG Maritime Pvt Ltd | M.V. Hampton Ocean | Bulk Carriers | 43291 | Second Officer | 01/03/2026 | 18/03/2026'


def test_extract_pdf_uses_real_table_grid_for_gilfred_service_record():
    """
    End-to-end confirmation against the real Gilfred Chettiar PDF (not
    synthetic data) -- the exact real bug: Company Name and Vessel Name
    were completely absent from the old word-coordinate-reconstructed
    text for this candidate's Seafarer's Service Record table. Skips if
    the real candidate fixture isn't present in this environment.
    """
    path = (
        '/Users/shreya/Desktop/marine-ats/data/myc_resume/'
        '1. 2024 Candidates/Gilfred Chettiar/GilfredChettiarResume.pdf'
    )
    if not os.path.exists(path):
        import pytest
        pytest.skip('Real candidate fixture not present in this environment')

    result = extract_pdf(path)
    text = result['raw_text']
    assert 'Company Name | Vessel Name' in text
    assert 'AG Maritime Pvt Ltd | M.V. Hampton Ocean' in text
    # The real header regression: the table must land in the AI-visible
    # "experience" section, not get lost/miscategorized (see
    # pipeline/parser.py's SECTION_HEADERS "Seafarer's Service Record" fix).
    from pipeline.parser import parse_resume
    parsed = parse_resume(text)
    assert 'Company Name | Vessel Name' in parsed['sections']['experience']
