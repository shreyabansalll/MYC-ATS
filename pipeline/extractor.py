# pipeline/extractor.py
import io
import re
import pdfplumber
import pymupdf
from pdfminer.pdfdocument import PDFPasswordIncorrect
from PIL import Image as PILImage
from docx import Document


_GLUED_ALNUM_PATTERN = re.compile(r"^([A-Za-z][A-Za-z'\-]{1,})(\d{10,})$")


def _split_glued_words(words: list) -> list:
    """
    Splits a single pdfplumber word that's actually two unrelated fields
    glued together with zero character-gap in the source PDF — e.g.
    'Training10502363112502312' (confirmed on a real resume's Courses
    table: an institute name wraps onto a second line ending in
    "Training", which sits immediately adjacent to that course's
    reference number with no separating whitespace character at all in
    the PDF's own content stream). pdfplumber's word-boundary detection
    only ever splits on a horizontal gap between characters — when the
    source PDF places two logically distinct fields with no gap
    whatsoever, they arrive as one token, and no amount of downstream
    line-grouping or column-splitting can separate them, since by that
    point it's already a single word with a single x0/x1 span.

    Detected by shape only — letters immediately followed by a 10+ digit
    run, nothing else. Reference numbers in these resumes are
    consistently 13-17 digits; "letters immediately followed by a
    10-digit-plus run" is not a real English word pattern this could
    plausibly false-positive on. Splits the x-span proportionally by
    character count so the two halves still sort/group correctly by
    position — exact sub-pixel accuracy doesn't matter here, both halves
    land at nearly the same x either way.
    """
    result = []
    for w in words:
        m = _GLUED_ALNUM_PATTERN.match(w['text'])
        if not m:
            result.append(w)
            continue
        letters, digits = m.group(1), m.group(2)
        split_frac = len(letters) / len(w['text'])
        mid_x = w['x0'] + (w['x1'] - w['x0']) * split_frac
        result.append({**w, 'text': letters, 'x1': mid_x})
        result.append({**w, 'text': digits, 'x0': mid_x})
    return result


def _words_to_text(words: list, x_min: float, x_max: float, y_tolerance: float = 3.0) -> str:
    """
    Converts pdfplumber word objects within an x-range to clean text.
    Groups words by y-coordinate (visual line), sorts each line left-to-right.
    This gives correct reading order regardless of PDF stream order — critical
    for Canva two-column templates where stream order interleaves columns.
    """
    # Filter words to x range
    col_words = [w for w in words if x_min <= float(w['x0']) < x_max]
    if not col_words:
        return ''

    # Group by y-position (words within y_tolerance of each other = same line).
    # Compares each word to the PREVIOUS word's y, not the line's first word —
    # anchoring to the first word let small cumulative baseline drift across a
    # long line (justified text, mixed bold/regular font metrics) silently
    # split that line mid-sentence once drift exceeded y_tolerance from the
    # anchor, even though each word was within tolerance of its neighbor.
    col_words.sort(key=lambda w: float(w['top']))
    lines = []
    current_line = [col_words[0]]
    current_y = float(col_words[0]['top'])

    for word in col_words[1:]:
        word_y = float(word['top'])
        if abs(word_y - current_y) <= y_tolerance:
            current_line.append(word)
        else:
            lines.append(current_line)
            current_line = [word]
        current_y = word_y
    lines.append(current_line)

    # Sort each line left-to-right and join
    text_lines = []
    for line in lines:
        line.sort(key=lambda w: float(w['x0']))
        text_lines.append(' '.join(w['text'] for w in line))

    return '\n'.join(text_lines)


def _detect_gutter_split(words: list, x0: float, x1: float, min_gap: float = 15.0) -> float:
    """
    Finds the true column gutter by looking for an x-range that contains
    NO word content anywhere on the page — a genuine empty gap between
    two columns — rather than guessing a fixed fraction of page width.

    Why this matters: the existing candidate loop below tries fixed split
    ratios (0.45-0.65) and picks whichever scores best. That guessing
    approach can land the split in the middle of a wide single-column
    paragraph whose wrapped lines extend past the guess, so
    _words_to_text() (which filters individual words by x-position before
    grouping into lines) ends up slicing that paragraph's lines in half —
    confirmed on a real resume, where a Summary paragraph's wrapped lines
    lost words like "solid foundation in" and "problem-solving" mid-
    sentence because every guessed ratio cut across them (see
    tests/test_extractor.py).

    A genuine gutter is provably empty of word content across the WHOLE
    page (every word, every line), so a paragraph that truly belongs to
    one column can never have a word crossing it — filtering words
    against a detected true gutter can't fragment a line that never
    reaches into it, unlike a guessed ratio.

    A single-column header row above the two-column body (name, phone,
    email, DOB on one line) breaks that assumption: such a row isn't
    confined to either column, so one of its words can happen to land
    inside what is, further down the page, the true gutter — bridging
    and destroying the gap when intervals are merged globally regardless
    of row. Confirmed on a real resume: a lone date-of-birth
    ("21/12/1999") on the header row extended 19pt into the real 24pt
    gutter, collapsing it to 5pt (below min_gap), so no qualifying gap
    was found at all and this function silently fell back to the page
    midpoint — which sat to the LEFT of several genuine left-column
    words' x0, misclassifying them into the right column (see
    tests/test_extractor.py for the exact reproduction).

    Guarding against this by unconditionally excluding words that straddle
    the page's rough midpoint was tried and reverted: it also excludes
    legitimate words in a genuinely wide (non-50/50) column — e.g. an
    87%/13% layout has plenty of real left-column words straddling the
    page's center — which regressed correctly-working detection on
    asymmetric layouts. Instead, this only kicks in as a fallback: try
    the plain global merge first (unchanged, so every previously-working
    case behaves exactly as before); only if THAT finds no qualifying gap
    at all — the specific failure mode a bridging header word causes — is
    the same search retried with straddling words excluded. A genuine
    column-confined word never straddles the page's center; only
    header-row content wide enough to cross it can, so this second pass
    only removes the kind of word that caused the first pass to fail.

    Returns the midpoint of the widest qualifying gap found in the
    plausible column-boundary region (10%-90% of page width — wide
    enough to cover narrow-sidebar templates where the right column is a
    small fraction of the page, while still excluding the page's own
    margins). Returns the page midpoint if no clear gutter is found even
    on the fallback pass — callers should treat that as a low-confidence
    result and keep the guessed-ratio candidates in play alongside it.
    """
    if not words:
        return x0 + (x1 - x0) * 0.5

    region_lo = x0 + (x1 - x0) * 0.10
    region_hi = x0 + (x1 - x0) * 0.90

    def _widest_gap(candidate_words):
        intervals = sorted(
            ((float(w['x0']), float(w['x1'])) for w in candidate_words),
            key=lambda iv: iv[0],
        )
        merged = [intervals[0]]
        for s, e in intervals[1:]:
            last_s, last_e = merged[-1]
            if s <= last_e:
                merged[-1] = (last_s, max(last_e, e))
            else:
                merged.append((s, e))

        best_gap, best_mid = 0.0, None
        for (_, e1), (s2, _) in zip(merged, merged[1:]):
            gap = s2 - e1
            mid = (e1 + s2) / 2
            if gap >= min_gap and region_lo <= mid <= region_hi and gap > best_gap:
                best_gap, best_mid = gap, mid
        return best_mid

    result = _widest_gap(words)
    if result is not None:
        return result

    rough_mid = x0 + (x1 - x0) * 0.5
    non_straddling = [
        w for w in words
        if not (float(w['x0']) < rough_mid < float(w['x1']))
    ]
    if non_straddling:
        result = _widest_gap(non_straddling)
        if result is not None:
            return result

    return x0 + (x1 - x0) * 0.5


def _table_has_real_gridlines(page, table) -> bool:
    """
    pdfplumber's table-detection heuristics can produce a spurious
    "table" from mere text-block alignment even with zero actual
    ruling lines on the page -- confirmed on a real resume: Ankur
    Shrivastava's page 0 (a Summary/Technical Expertise page with no
    real table at all) was detected as a 2-row, 1-col "table" spanning
    almost the entire page, purely from rect-based heuristics, despite
    page.lines being completely empty there. A genuine gridded table --
    the kind this project's real Seafarer's Service Record tables
    consistently have (confirmed via direct inspection: 46-66 real
    ruling lines, 8 real columns) -- has actual ruling lines on the
    page and multiple real columns; a false positive from whitespace/
    rect heuristics alone doesn't clear both bars at once.
    """
    if not page.lines:
        return False
    if len(table.rows) < 3:
        return False
    if not table.rows or len(table.rows[0].cells) < 3:
        return False
    return True


def _render_table_as_text(table_data: list) -> str:
    """
    Renders a pdfplumber-extracted table (list of row lists) as clean,
    pipe-delimited text -- one row per line, every column preserved
    and unambiguous. Confirmed real bug this replaces: the existing
    word-coordinate/layout-based reconstruction can lose entire
    columns outright on a real resume's Seafarer's Service Record
    table (Gilfred Chettiar: "Company Name" and "Vessel Name" were
    completely absent from the extracted text -- multi-line cell
    wrapping, e.g. "Bulk\\nCarriers", broke the row-grouping heuristic
    that reconstruction relies on), which starves the AI rewrite
    prompt of company/vessel data it has no other source for on some
    candidates -- a direct, confirmed contributor to fabricated
    company names and blank vessel fields in real generated output.
    extract_tables() pairs every cell correctly regardless of
    multi-line wrapping, since it reads the table's actual ruling-line
    grid rather than inferring rows from word y-coordinates.
    """
    lines = []
    for row in table_data:
        cells = [(str(c).replace('\n', ' ').strip() if c else '') for c in row]
        if not any(cells):
            continue
        lines.append(' | '.join(cells))
    return '\n'.join(lines)


def _extract_real_tables(page):
    """
    Finds genuine gridded tables on this page (see
    _table_has_real_gridlines) and returns (rendered_text, word_bboxes)
    -- the clean pipe-delimited text for every real table found, and
    the list of (x0, top, x1, bottom) bounding boxes to exclude from
    the word-coordinate reconstruction pass below, so the same content
    isn't extracted twice (once cleanly via the table grid, once
    scrambled via word-coordinates).
    """
    tables = page.find_tables()
    real_tables = [t for t in tables if _table_has_real_gridlines(page, t)]
    if not real_tables:
        return '', []

    rendered = '\n\n'.join(_render_table_as_text(t.extract()) for t in real_tables)
    bboxes   = [t.bbox for t in real_tables]
    return rendered, bboxes


def _word_in_any_bbox(word, bboxes) -> bool:
    wx0, wtop, wx1, wbottom = float(word['x0']), float(word['top']), float(word['x1']), float(word['bottom'])
    for bx0, btop, bx1, bbottom in bboxes:
        # A word "belongs" to a table region if its center falls inside
        # the table's bbox -- more robust than requiring full containment,
        # since a word right at a table's edge can have a bbox that
        # technically pokes a fraction of a point outside it.
        cx, cy = (wx0 + wx1) / 2, (wtop + wbottom) / 2
        if bx0 <= cx <= bx1 and btop <= cy <= bbottom:
            return True
    return False


def _column_bleed_penalty(text: str) -> int:
    """
    Counts short, isolated, non-heading lines in a reconstructed column's
    text -- the actual signal that distinguishes a cleanly-separated
    column from one where a handful of words from the OTHER column got
    swept in. Confirmed as a real gap in the previous scoring heuristic
    (heading-keyword count + total combined length): reassigning a word
    from one column-bucket to the other doesn't remove it or any
    heading, so both of those scores stay identical across a badly-bled
    split and a clean one -- the previous version of this function had
    no way to prefer the clean split over the bled one, and confirmed on
    a real resume, the WRONG (bled) split won purely because it happened
    to be first in the candidate list and every candidate tied on both
    scores.

    A word that only READS correctly as part of the column it was
    actually written in has real neighbors on the same line in that
    column; swept into the wrong column, it has no such neighbor (the
    real neighbor stayed behind), so it lands alone on its own line once
    grouped by y-position. A handful of stray one-word lines like "in",
    "and", "hands-" is exactly what that produces; a genuinely clean
    column's lines are either multi-word content or a short (usually
    ALL-CAPS) heading.
    """
    penalty = 0
    for line in text.split('\n'):
        stripped = line.strip()
        if not stripped:
            continue
        words = stripped.split()
        if len(words) <= 1 and not stripped.isupper() and len(stripped) < 15:
            penalty += 1
    return penalty


def _clean_text(text: str) -> str:
    """Removes orphan single-char lines and collapses excessive blank lines."""
    lines = text.split('\n')
    cleaned = []
    for line in lines:
        stripped = line.strip()
        if len(stripped) == 1 and stripped.isalpha():
            continue
        cleaned.append(line.rstrip())
    text = '\n'.join(cleaned)
    return re.sub(r'\n{3,}', '\n\n', text)


def extract_text(file_path: str, ext: str) -> dict:
    if ext == 'pdf':
        return extract_pdf(file_path)
    elif ext == 'docx':
        return extract_docx(file_path)
    else:
        return {
            'raw_text':          '',
            'structural_issues': ['UNSUPPORTED_FILE_TYPE'],
            'is_ocr':            False,
        }


def extract_pdf(path: str) -> dict:
    text = ''
    issues = []
    is_multi_column = False

    try:
        with pymupdf.open(path) as probe:
            if probe.needs_pass:
                return {
                    'raw_text':          '',
                    'structural_issues': ['PASSWORD_PROTECTED_PDF'],
                    'is_ocr':            False,
                }

        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                x0   = page.bbox[0]
                y0   = page.bbox[1]
                x1   = page.bbox[2]
                y1   = page.bbox[3]
                mid_x = x0 + (x1 - x0) * 0.5

                words = page.extract_words()
                if not words:
                    page_text = page.extract_text() or ''
                    if page_text:
                        text += page_text + '\n'
                    continue

                # Real gridded tables (Seafarer's Service Record, etc.) are
                # extracted via their actual ruling-line grid FIRST, not the
                # word-coordinate reconstruction below -- confirmed real
                # data-loss bug (Gilfred Chettiar): the word-coordinate path
                # lost the Company Name and Vessel Name columns entirely on
                # a real service-record table (multi-line cell wrapping
                # broke its row-grouping heuristic), while extract_tables()
                # pairs every cell correctly regardless of wrapping, since
                # it reads the grid directly rather than inferring rows from
                # word y-coordinates. Table-covered words are excluded from
                # `words` below so the same content isn't reconstructed a
                # second time, scrambled, alongside the clean version.
                table_text, table_bboxes = _extract_real_tables(page)
                # A page-level view with table regions removed -- used for
                # both extract_text() fallback calls below (the raw
                # candidate-loop fallback and the single-column branch),
                # since neither one reads from the filtered `words` list;
                # without this, a table's scrambled raw text would still
                # get pulled back in through whichever of them fires.
                text_page = page.filter(lambda obj: not _word_in_any_bbox(obj, table_bboxes)) if table_bboxes else page
                if table_bboxes:
                    words = [w for w in words if not _word_in_any_bbox(w, table_bboxes)]

                if not words:
                    # Page is essentially pure table (e.g. a dedicated
                    # service-record page) -- nothing left to reconstruct.
                    if table_text:
                        text += table_text + '\n'
                    continue

                words = _split_glued_words(words)

                left_words  = [w for w in words if float(w['x0']) < mid_x]
                right_words = [w for w in words if float(w['x0']) >= mid_x]
                left_ratio  = len(left_words) / max(len(words), 1)
                is_two_col  = 0.25 < left_ratio < 0.75 and len(right_words) > 10

                if is_two_col:
                    is_multi_column = True
                    best_text    = ''
                    best_count   = 0
                    best_penalty = None

                    # Candidate reconstructions to try, best-scoring wins.
                    # The detected-gutter candidate goes first so it wins
                    # ties (see _detect_gutter_split's docstring): a split
                    # point derived from an actual empty gap in the page's
                    # word content is structurally more robust than a
                    # guessed fixed ratio, since it can't land in the middle
                    # of a wide paragraph. The fixed ratios remain as a
                    # tested fallback — they only override the detected-
                    # gutter result if they score strictly higher.
                    detected_split = _detect_gutter_split(words, x0, x1)
                    candidates = [(
                        _words_to_text(words, x0, detected_split),
                        _words_to_text(words, detected_split, x1 + 1),
                    )]
                    for ratio in [0.45, 0.50, 0.55, 0.60, 0.65]:
                        split = x0 + (x1 - x0) * ratio
                        # Use word-based extraction — avoids stream-order interleaving
                        candidates.append((
                            _words_to_text(words, x0, split),
                            _words_to_text(words, split, x1 + 1),
                        ))

                    for left_text, right_text in candidates:
                        left_text  = _clean_text(left_text)
                        right_text = _clean_text(right_text)
                        combined   = left_text + '\n' + right_text
                        combined   = _clean_text(combined)

                        count = sum(1 for h in [
                            'summary', 'experience', 'education', 'skills',
                            'certifications', 'license', 'documents', 'courses',
                        ] if h in combined.lower())
                        # Real bug fixed: heading-count and text length are
                        # both invariant to WHICH column a bled word lands
                        # in (no word is added or dropped, just reassigned),
                        # so they alone can't tell a cleanly-separated split
                        # from a badly-bled one -- confirmed on a real
                        # resume where the wrong split won purely because
                        # every candidate tied on both scores and it
                        # happened to be first. The bleed penalty (see
                        # _column_bleed_penalty) is the actual tie-breaker:
                        # prefer more headings found, then fewer stray
                        # swept-in words, then longer text as a final,
                        # rarely-reached tiebreak.
                        penalty = _column_bleed_penalty(left_text) + _column_bleed_penalty(right_text)

                        is_better = (
                            count > best_count
                            or (count == best_count and (best_penalty is None or penalty < best_penalty))
                            or (count == best_count and penalty == best_penalty and len(combined) > len(best_text))
                        )
                        if is_better:
                            best_count   = count
                            best_penalty = penalty
                            best_text    = combined

                    page_text = best_text if best_text else (text_page.extract_text() or '')

                else:
                    # layout=True preserves absolute vertical position via
                    # blank-line padding -- normally harmless, but excluding
                    # a large table's words leaves a correspondingly large
                    # hole of blank space where it used to sit. _clean_text()
                    # collapses that (never applied to this branch before,
                    # since a plain page's padding was never large enough to
                    # matter) -- real downstream cost otherwise:
                    # sanitise_section()'s fixed character budget would burn
                    # a chunk of it on blank lines instead of real content
                    # before this text ever reaches the AI rewrite prompt.
                    page_text = _clean_text(text_page.extract_text(layout=True) or text_page.extract_text() or '')

                if page_text and table_text:
                    page_text = page_text + '\n' + table_text
                elif table_text:
                    page_text = table_text

                if page_text:
                    text += page_text + '\n'

    except PDFPasswordIncorrect:
        # Real gap fixed, confirmed against an actual password-protected
        # PDF: pdfminer raises this with an EMPTY message ('') -- the
        # generic handler below produced the unhelpful, uninformative
        # 'PDF_READ_ERROR: ' with nothing after the colon. A password-
        # protected upload is common enough (some export tools encrypt by
        # default) and specific enough to deserve its own clear signal,
        # the same way SCANNED_PDF_DETECTED_OCR_NEEDED gets one below --
        # both are "not a crash, a known unreadable-input shape" cases.
        return {
            'raw_text':          '',
            'structural_issues': ['PASSWORD_PROTECTED_PDF'],
            'is_ocr':            False,
        }
    except Exception as e:
        return {
            'raw_text':          '',
            'structural_issues': [f'PDF_READ_ERROR: {str(e)}'],
            'is_ocr':            False,
        }

    if len(text.strip()) < 100:
        return {
            'raw_text':          text,
            'structural_issues': ['SCANNED_PDF_DETECTED_OCR_NEEDED'],
            'is_ocr':            True,
        }

    column_severity = 'UNKNOWN'
    if is_multi_column:
        issues.append('MULTI_COLUMN_LAYOUT_DETECTED')
        column_severity = detect_column_severity(text)

    return {
        'raw_text':          text.strip(),
        'structural_issues': issues,
        'is_ocr':            False,
        'column_severity':   column_severity,
    }


def _normalize_for_docx(image_bytes: bytes):
    """
    Re-encodes raw PDF-embedded image bytes through Pillow before they
    ever reach python-docx. Confirmed on a real extracted photo: PyMuPDF
    returns the JPEG exactly as embedded in the PDF's XObject stream —
    valid JPEG data (a real image viewer opens it fine), but starting
    directly with SOI + DQT and no JFIF/APP0 marker segment, since PDF
    generators commonly omit it. python-docx's own minimal image-header
    sniffer requires that marker to recognize a JPEG at all and raises
    UnrecognizedImageError on this otherwise-valid file. Re-saving
    through Pillow guarantees a standard JFIF header regardless of what
    the source PDF omitted.

    Returns the re-encoded JPEG bytes, or None if Pillow can't decode
    the input at all (a genuinely corrupt/unsupported embedded image) —
    the caller treats that the same as "no photo found".
    """
    try:
        with PILImage.open(io.BytesIO(image_bytes)) as im:
            im = im.convert('RGB')
            buf = io.BytesIO()
            im.save(buf, format='JPEG', quality=90)
            return buf.getvalue()
    except Exception:
        return None


def extract_photo(pdf_path: str):
    """
    Best-effort candidate photo extraction from page 1 of the source PDF
    — for the photo-included DOCX variant only (see
    pipeline/generator.py's add_candidate_photo). Not part of
    extract_text()'s contract: this is an independent, optional lookup
    callers make only when building the photo variant, so a source with
    no photo (or a DOCX upload, which this doesn't handle at all) never
    affects the main extraction path.

    Picks the most photo-like image on page 1 by simple heuristics —
    portrait-ish aspect ratio, a plausible headshot size, positioned in
    the top third of the page (the near-universal placement for a resume
    photo, confirmed on a real candidate PDF: page 1, x0=486 x1=565
    top=27 bottom=107) — since a resume PDF's images can also be a
    company logo or a decorative banner, which this must not mistake for
    a candidate photo.

    Returns raw image bytes (as embedded in the PDF — usually JPEG) or
    None if there's no PDF, no qualifying image, or anything about
    extraction fails. Never raises — a missing/unreadable photo means
    the photo variant is skipped for this candidate, not a pipeline
    failure.
    """
    if not pdf_path or not pdf_path.lower().endswith('.pdf'):
        return None

    try:
        doc = pymupdf.open(pdf_path)
        try:
            if doc.page_count == 0:
                return None
            page   = doc[0]
            page_h = page.rect.height

            best_bytes, best_score = None, -1.0
            for img in page.get_images(full=True):
                xref = img[0]
                rects = page.get_image_rects(xref)
                if not rects:
                    continue
                rect = rects[0]
                w, h = rect.width, rect.height
                if w <= 0 or h <= 0:
                    continue
                aspect       = w / h
                in_top_third = rect.y0 < page_h * 0.35
                plausible_size = 40 <= w <= 250 and 40 <= h <= 250
                portrait_ish   = 0.55 <= aspect <= 1.4
                if not (in_top_third and plausible_size and portrait_ish):
                    continue
                # Most square (most headshot-like) candidate wins when a
                # page has more than one qualifying image.
                score = -abs(1.0 - aspect)
                if score > best_score:
                    base = doc.extract_image(xref)
                    best_bytes, best_score = base.get('image'), score

            return _normalize_for_docx(best_bytes) if best_bytes else None
        finally:
            doc.close()
    except Exception:
        return None


def extract_docx(path: str) -> dict:
    issues = []
    text   = ''

    try:
        doc = Document(path)

        for para in doc.paragraphs:
            if para.text.strip():
                text += para.text + '\n'

        if doc.tables:
            issues.append('TABLES_DETECTED')
            for table in doc.tables:
                for row in table.rows:
                    for cell in row.cells:
                        if cell.text.strip():
                            text += cell.text + '\n'

        for section in doc.sections:
            header_text = ' '.join([p.text for p in section.header.paragraphs]).strip()
            footer_text = ' '.join([p.text for p in section.footer.paragraphs]).strip()
            if header_text:
                issues.append('CONTENT_IN_HEADER')
            if footer_text:
                issues.append('CONTENT_IN_FOOTER')

        if doc.inline_shapes:
            issues.append('IMAGES_OR_GRAPHICS_DETECTED')

    except Exception as e:
        return {
            'raw_text':          '',
            'structural_issues': [f'DOCX_READ_ERROR: {str(e)}'],
            'is_ocr':            False,
        }

    return {
        'raw_text':          text.strip(),
        'structural_issues': issues,
        'is_ocr':            False,
    }


def detect_column_severity(raw_text: str) -> str:
    lines       = [l.strip() for l in raw_text.split('\n') if l.strip()]
    short       = sum(1 for l in lines if len(l) < 30)
    total       = len(lines)
    if total == 0:
        return 'UNKNOWN'
    short_ratio = short / total
    if short_ratio > 0.6:    return 'SEVERE'
    elif short_ratio > 0.35: return 'MODERATE'
    else:                    return 'MILD'