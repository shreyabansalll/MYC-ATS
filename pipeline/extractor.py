# pipeline/extractor.py
import re
import pdfplumber
from docx import Document


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

    # Group by y-position (words within y_tolerance of each other = same line)
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

    Returns the midpoint of the widest qualifying gap found in the
    plausible column-boundary region (10%-90% of page width — wide
    enough to cover narrow-sidebar templates where the right column is a
    small fraction of the page, while still excluding the page's own
    margins). Returns the page midpoint if no clear gutter is found —
    callers should treat that as a low-confidence fallback and keep the
    guessed-ratio candidates in play alongside it.
    """
    if not words:
        return x0 + (x1 - x0) * 0.5

    intervals = sorted(
        ((float(w['x0']), float(w['x1'])) for w in words),
        key=lambda iv: iv[0],
    )
    merged = [intervals[0]]
    for s, e in intervals[1:]:
        last_s, last_e = merged[-1]
        if s <= last_e:
            merged[-1] = (last_s, max(last_e, e))
        else:
            merged.append((s, e))

    region_lo = x0 + (x1 - x0) * 0.10
    region_hi = x0 + (x1 - x0) * 0.90

    best_gap = 0.0
    best_mid = None
    for (_, e1), (s2, _) in zip(merged, merged[1:]):
        gap = s2 - e1
        mid = (e1 + s2) / 2
        if gap >= min_gap and region_lo <= mid <= region_hi and gap > best_gap:
            best_gap = gap
            best_mid = mid

    return best_mid if best_mid is not None else x0 + (x1 - x0) * 0.5


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

                left_words  = [w for w in words if float(w['x0']) < mid_x]
                right_words = [w for w in words if float(w['x0']) >= mid_x]
                left_ratio  = len(left_words) / max(len(words), 1)
                is_two_col  = 0.25 < left_ratio < 0.75 and len(right_words) > 10

                if is_two_col:
                    is_multi_column = True
                    best_text  = ''
                    best_count = 0

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

                        if count > best_count or (
                            count == best_count and len(combined) > len(best_text)
                        ):
                            best_count = count
                            best_text  = combined

                    page_text = best_text if best_text else (page.extract_text() or '')

                else:
                    page_text = page.extract_text(layout=True) or page.extract_text() or ''

                if page_text:
                    text += page_text + '\n'

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