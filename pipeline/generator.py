# pipeline/generator.py
# STAGE 7 — ATS-Compliant DOCX Generator
# Takes rewritten JSON from services/ai_client.py
# Builds a clean single-column ATS-friendly DOCX
# Library: python-docx
# Rules applied: R1-R15 (format), R16-R25 (sections) from ATS PDF

import io
import re
from docx import Document
from docx.shared import Pt, Inches, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
import os
from config import OUTPUT_DIR


# ── Rank groups for page length control ──────────────────────────────
SINGLE_PAGE_RANKS = {
    'deck cadet', 'engine cadet', 'oiler', 'motorman',
    'fitter', 'ab seaman', 'able seaman', 'bosun', 'jwko'
}
TWO_PAGE_RANKS = {
    'third officer', 'second officer', 'third engineer',
    'fourth engineer', 'eto', 'second engineer'
}
# master, chief officer, chief engineer → 2 pages, more vessel entries allowed

# Overall rendered word-count targets, on top of the per-field caps below.
# Per-field caps alone (skills count, bullets-per-entry) can't catch a
# resume that overflows its page budget because individual bullets are
# long, not numerous — confirmed on real generated output. ~500-600
# words is a reasonable single 11pt page with 1" margins (matching the
# existing single-page/two-page rank split above); two pages roughly
# doubles that. These are reasoned estimates, not tuned against the
# validation set — flag for review if real DOCX output still overflows
# its target page count after this.
WORD_LIMIT_SINGLE_PAGE = 600
WORD_LIMIT_TWO_PAGE    = 1150


_XML_ILLEGAL_CHARS = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')


def _xml_safe(text) -> str:
    """
    Strips XML-illegal control characters (the C0 control codes lxml's
    Element.text setter rejects outright, tab/LF/CR excepted) before any
    text reaches a python-docx run. Confirmed as a real crash: a
    certification line pulled verbatim from a garbled two-column PDF
    extraction (services/ai_client.py's enforce_cert_completeness safety
    net appends original source lines when the AI rewrite drops one) can
    carry a stray control byte that AI-generated text never would, and
    python-docx/lxml raises ValueError on it rather than dropping it
    silently — a crashed job is strictly worse than the incomplete
    output the safety net exists to prevent. Applied at the point of
    writing to the document rather than at extraction, so every text
    field is covered regardless of which upstream stage it came from.
    """
    return _XML_ILLEGAL_CHARS.sub('', str(text))


def _as_list(value) -> list:
    """
    Defensive coercion for LLM output that doesn't follow the expected
    shape (e.g. a string instead of a list for skills/certifications/
    education/languages) — applied consistently across all list-shaped
    content fields rather than ad hoc per field.
    """
    if isinstance(value, list):
        return value
    return [value] if value else []


def _job_field(job: dict, key: str) -> str:
    """
    Reads a scalar experience_bullets field (role/company/dates) coerced
    to a plain string. Real crash fixed: add_sea_service_section's
    duplicate-row check uses (dates, company, headline) as a set key,
    which raises TypeError: unhashable type on a real Groq response
    where one of these fields came back as a list instead of a string
    (schema drift, same category as the dict-shaped certification
    entries _stringify_cert already guards against) -- confirmed on a
    real live run, this crashed DOCX generation entirely, losing an
    otherwise-successful AI rewrite. A list value is joined into a
    single readable string rather than raising or silently dropping it.
    """
    value = job.get(key, '')
    if isinstance(value, list):
        return ', '.join(str(v) for v in value if v not in (None, ''))
    return str(value) if value is not None else ''


_ABBREVIATION_IN_PROGRESS = re.compile(r'^[A-Z](\.[A-Z])*$')


def split_into_sentences(text: str) -> list:
    """
    Splits text into sentences on '.', '!', '?' — but treats a period
    immediately following a single capital letter (e.g. 'M.T', 'M.V' —
    standard maritime vessel-prefix abbreviations for Motor Tanker/Motor
    Vessel) as not a sentence boundary. A naive split('.') here truncates
    summaries mid-abbreviation: confirmed on real generated output where
    'Sailed on M.T Example GRT 12345...' got cut to '...Sailed on M.T'
    once the 3-sentence cap kicked in after only two real sentences.

    Real bug fixed, confirmed on different real generated output: the
    original single-letter check only guarded ONE period per
    abbreviation, so 'M.V.' (two periods -- 'Motor Vessel' written with
    a period after EACH letter, as opposed to 'M.T' with only one) still
    tripped a false sentence boundary on its SECOND period, once the
    accumulated fragment became the two-character "word" 'M.V' instead
    of the single-character 'M' the original check looked for --
    confirmed as a real truncated summary ending mid-vessel-name,
    "...on M.V. FEDERAL ST LAURENT and M.V." The check now matches the
    whole abbreviation-so-far (one or more single uppercase letters each
    followed by a period, e.g. 'M', then 'M.V', then 'M.V.T' if it kept
    going) rather than only its first letter, so it keeps not-splitting
    across every period in a multi-letter abbreviation, not just the first.
    """
    if not text:
        return []
    if not isinstance(text, str):
        text = str(text)  # defensive: a non-string caller shouldn't crash text.replace() below
    text = text.replace('!', '.').replace('?', '.')
    tokens = text.split('.')
    sentences = []
    current = ''
    for i, tok in enumerate(tokens):
        current += tok
        if i == len(tokens) - 1:
            break
        last_word = current.split()[-1] if current.split() else ''
        if _ABBREVIATION_IN_PROGRESS.match(last_word):
            current += '.'   # part of an abbreviation like "M." or "M.V." — don't split
            continue
        sentences.append(current.strip())
        current = ''
    if current.strip():
        sentences.append(current.strip())
    return [s for s in sentences if s]


def _count_words(content: dict) -> int:
    """
    Approximate rendered word count across every section generate_docx()
    actually writes to the page — summary, skills, certifications,
    education, languages, every role's title/company/bullets, and the
    Sea Service Record section. Sea Service Record adds one real
    rendered line per experience entry (plus its own heading and totals
    line) even though it's compact — a real bug fixed here: an earlier
    version of this function excluded it entirely on the reasoning that
    it restates experience_bullets data already counted, but a separate
    section with its own heading and rows costs real vertical space on
    the page regardless of word overlap, and excluding it undercounted
    actual page usage (confirmed on real rendered output: a resume that
    should have fit one page spilled a couple of lines onto a mostly-
    empty second page). ~8 words is a reasoned per-row estimate for
    "MM/YYYY - MM/YYYY — Vessel (GRT nnnnn) — Company", not a literal count.
    """
    total = len(str(content.get('summary', '')).split())
    for skill in _as_list(content.get('skills', [])):
        total += len(str(skill).split())
    for cert in _as_list(content.get('certifications', [])):
        total += len(str(cert).split())
    for edu in _as_list(content.get('education', [])):
        total += len(str(edu).split())
    for lang in _as_list(content.get('languages', [])):
        total += len(str(lang).split())
    experience_entries = _as_list(content.get('experience_bullets', []))
    if experience_entries:
        total += 10  # "Sea Service Record" heading + totals/vessel-types line
        total += 8 * len(experience_entries)
    for job in experience_entries:
        if not isinstance(job, dict):
            continue
        total += len(str(job.get('role', '')).split())
        total += len(str(job.get('company', '')).split())
        for bullet in job.get('bullets', []):
            total += len(str(bullet).split())
    return total


def enforce_length_limits(content: dict, rank: str) -> dict:
    rank_lower = rank.lower() if rank else ''
    is_junior = rank_lower in SINGLE_PAGE_RANKS

    # Skills: 15 for junior, 18 for senior
    max_skills = 15 if is_junior else 18
    content['skills'] = _as_list(content.get('skills', []))[:max_skills]

    # Summary: 3 sentences max always
    summary = content.get('summary', '')
    sentences = split_into_sentences(summary)
    content['summary'] = '. '.join(sentences[:3]) + ('.' if sentences else '')

    # Experience: limit entries and bullets per entry
    experience = _as_list(content.get('experience_bullets', []))
    max_entries = 3 if is_junior else 6
    experience = experience[:max_entries]
    for job in experience:
        if isinstance(job, dict):
            job['bullets'] = job.get('bullets', [])[:3]
    content['experience_bullets'] = experience

    # Certifications: coerced to a list, but never count-capped or word-
    # budget-trimmed (see the loops below) — nothing a candidate has
    # earned may be silently dropped for page-length reasons (a
    # certificate is the #1 thing a recruiter screens for; see CLAUDE.md's
    # Project Overview). A hardcoded [:8] cap used to drop real entries
    # here — confirmed on real output: 2 of Mrinal Thapa's 10 real
    # certs/courses were silently truncated by this exact cap even though
    # services/ai_client.py's enforce_cert_completeness() had correctly
    # preserved all 10 through the rewrite. Page length is now managed
    # entirely by trimming skills/bullets (below) and, as a last resort,
    # compact_mode (see the flag set at the end of this function).
    content['certifications'] = _as_list(content.get('certifications', []))

    # Overall page-length enforcement (total word count), not just the
    # per-field caps above — those alone don't catch a resume that
    # overflows because individual bullets/skills are long rather than
    # numerous. Trims the least fact-critical content first (skills),
    # then reduces bullets-per-entry as a last resort, with a floor of 2
    # bullets/entry — never drops an entire experience entry, since every
    # vessel/role entry from source must still appear (see
    # services/ai_client.py Rule 2: "Include ALL vessel entries from
    # source — do not drop any"), and never drops certifications (see
    # above).
    word_limit = WORD_LIMIT_SINGLE_PAGE if is_junior else WORD_LIMIT_TWO_PAGE

    while _count_words(content) > word_limit and len(content['skills']) > 10:
        content['skills'] = content['skills'][:-1]

    while _count_words(content) > word_limit and any(
        isinstance(job, dict) and len(job.get('bullets', [])) > 2
        for job in content['experience_bullets']
    ):
        for job in content['experience_bullets']:
            if isinstance(job, dict) and len(job.get('bullets', [])) > 2:
                job['bullets'] = job['bullets'][:-1]
                break

    # Compact mode: the last-resort lever once skills/bullets are already
    # at their floors and certifications (never trimmed, see above) are
    # long enough to still push the page over budget — "no resume should
    # ever go to the 3rd page, but don't skip info to prevent it" means
    # the fix has to be typesetting, not deletion. generate_docx() reads
    # this flag to drop body/bullet text a point smaller and tighten
    # paragraph spacing across the whole document, a standard resume-
    # fitting technique that keeps every fact intact.
    content['_compact_mode'] = _count_words(content) > word_limit

    return content


def add_horizontal_rule(paragraph):
    """
    Adds a thin bottom border under a paragraph.
    Used under section headings — replaces table-based dividers (ATS violation).
    """
    pPr = paragraph._p.get_or_add_pPr()
    pBdr = OxmlElement('w:pBdr')
    bottom = OxmlElement('w:bottom')
    bottom.set(qn('w:val'), 'single')
    bottom.set(qn('w:sz'), '4')
    bottom.set(qn('w:space'), '1')
    bottom.set(qn('w:color'), '2E75B6')
    pBdr.append(bottom)
    pPr.append(pBdr)


def add_section_heading(doc, text, compact=False):
    """
    Adds a section heading: Arial bold ALL CAPS + blue underline rule.
    No tables, no text boxes — pure paragraph with border. One point
    smaller and slightly less space-before in compact mode (see
    enforce_length_limits) — still clearly a heading, just tighter.
    """
    p = doc.add_paragraph()
    run = p.add_run(_xml_safe(text).upper())
    run.bold = True
    run.font.size = Pt(11 if compact else 12)
    run.font.name = 'Arial'
    run.font.color.rgb = RGBColor(0x2E, 0x75, 0xB6)
    p.paragraph_format.space_before = Pt(6 if compact else 8)
    p.paragraph_format.space_after = Pt(2)
    add_horizontal_rule(p)
    return p


def add_body_text(doc, text, bold=False, size=None, compact=False):
    """
    Adds a plain body paragraph — Arial, no special formatting. Explicit
    `size` always wins; otherwise 10pt in compact mode, 11pt normally.
    1pt space_after regardless of mode -- tightened from 2pt (normal
    mode) after confirming on a real rendered page that even well-
    under-budget content (word count, not visual space) was still
    spilling a couple of lines onto a nearly-empty extra page; this and
    the page-margin reduction above are what actually closed that gap.
    """
    if size is None:
        size = 10 if compact else 11
    p = doc.add_paragraph()
    run = p.add_run(_xml_safe(text))
    run.bold = bold
    run.font.size = Pt(size)
    run.font.name = 'Arial'
    p.paragraph_format.space_after = Pt(1)
    p.paragraph_format.space_before = Pt(0)
    return p


def add_bullet(doc, text, compact=False):
    """
    Adds a bullet point using Word's built-in List Bullet style — a real
    numbering-defined bullet glyph (confirmed via the generated DOCX's
    own numbering.xml: numFmt="bullet" on a Symbol-font level tied to
    this style), never a literal unicode character prefixed onto the
    text (ATS violation R11).
    """
    p = doc.add_paragraph(style='List Bullet')
    run = p.add_run(_xml_safe(text))
    run.font.size = Pt(10 if compact else 11)
    run.font.name = 'Arial'
    p.paragraph_format.space_after = Pt(1)
    return p


def add_candidate_photo(doc, photo_bytes, width_inches=0.85):
    """
    Embeds the candidate's photo as a standalone INLINE image in the
    document body — never in a header/footer element, never in a text
    box, never in a table, and never with text-wrap/floating placement.
    python-docx's add_picture() on a plain paragraph run is inherently
    inline (anchored in the text flow, not floating), which is what
    actually keeps this ATS-safe: the risk some ATS parsers flag is a
    wrapped/floating/positioned image or one embedded in a header —
    not the mere presence of an inline image in the body (see
    pipeline/extractor.py's extract_photo docstring and Priority 2's
    research).

    Right-aligned rather than centered, and sized down from an earlier
    1.0in/centered version — confirmed on a real rendered page that a
    large centered photo floating alone between the contact line and the
    summary, with generous space above and below, reads as an awkward
    afterthought rather than a deliberate header element. Right-aligned
    reads as "corner photo", the closest a strictly single-column,
    no-table, no-floating layout can get to the two-column photo-corner
    convention without violating those same ATS-safety constraints.
    """
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    p.paragraph_format.space_before = Pt(2)
    p.paragraph_format.space_after = Pt(4)
    run = p.add_run()
    run.add_picture(io.BytesIO(photo_bytes), width=Inches(width_inches))
    return p


def _extract_vessel_grt(bullets: list) -> str:
    """
    Best-effort 'Vessel Name (GRT nnnnn)' extraction from already-
    generated bullet text, for the Sea Service Record's compact per-role
    summary line (see add_sea_service_section). This never invents a
    vessel name — if a bullet doesn't clearly state one immediately
    before "GRT <number>" (the phrasing services/ai_client.py's Rule 2
    mandates), the detail is just omitted from the summary row rather
    than guessed; the full sentence is already present in Work
    Experience immediately below regardless, so nothing is lost, only
    the compact row is less complete for that one entry.
    """
    for bullet in bullets or []:
        text = str(bullet)
        m = re.search(r'\bGRT\s*[:\-]?\s*([\d,]+)', text, re.IGNORECASE)
        if not m:
            continue
        # Real bug fixed, confirmed on real output: "GRT 51917, ensuring
        # compliance..." (a comma immediately after the number, ordinary
        # sentence punctuation with no space before it) got captured as
        # part of the number itself, since [\d,]+ also matches a comma
        # for the legitimate case of a thousands-separated GRT value —
        # rendered as "(GRT 51917,)" with a dangling comma. Strip a
        # trailing comma specifically; a genuine "51,917"-style value
        # keeps its internal comma.
        grt = m.group(1).rstrip(',')
        # Strip trailing punctuation ("...Example Star, GRT 62795" is the
        # natural sentence phrasing Rule 2 asks for) — the name pattern's
        # end anchor otherwise never matches, since a comma/semicolon
        # right before "GRT" isn't part of either character class below.
        prefix = text[:m.start()].strip().rstrip(',;:')
        name_m = re.search(r'([A-Z][\w.\-]*(?:\s+[A-Z][\w.\-]*){0,4})\s*\(?\s*$', prefix)
        vessel = name_m.group(1).strip(' (') if name_m else ''
        return f'{vessel} (GRT {grt})' if vessel else f'GRT {grt}'
    return ''


def add_sea_service_section(doc, content, compact=False):
    """
    Recruiters scan sea service second, right after certificates (see
    CLAUDE.md's Project Overview: certs are the eligibility gate, sea
    service is the verification layer) — this section exists so that
    scan doesn't require reading full Work Experience prose first.
    Confirmed missing entirely from real generated output before this.

    Built from experience_bullets (role/company/dates), the only
    structured per-role data generate_docx actually receives — reverse-
    chronological order is trusted from the rewrite (services/
    ai_client.py Rule 2 requires it, and pipeline/scorer.py's R21 checks
    for it), not re-derived here.

    Deliberately NOT the same "Role — Company | Dates" line Work
    Experience's bold header shows immediately below — confirmed on a
    real rendered page that repeating it verbatim in both sections back
    to back reads as pure duplication, not two different views of the
    data. This row leads with dates and vessel/GRT (the fast-scan facts
    Work Experience doesn't put in its header line) at a smaller, plain-
    text size, so it reads as a compact index rather than a restatement.
    """
    experience = _as_list(content.get('experience_bullets', []))
    if not experience:
        return

    add_section_heading(doc, 'Sea Service Record', compact=compact)
    row_size = 9 if compact else 10

    sea_months   = content.get('sea_months')
    vessel_types = _as_list(content.get('vessel_types', []))
    summary_bits = []
    if sea_months:
        summary_bits.append(f'Total Sea Service: {sea_months} months')
    if vessel_types:
        summary_bits.append('Vessel Types: ' + ', '.join(str(v).title() for v in vessel_types))
    if summary_bits:
        add_body_text(doc, '  |  '.join(summary_bits), bold=True, size=row_size, compact=compact)

    seen_rows = set()
    for job in experience:
        if not isinstance(job, dict):
            continue
        dates   = _job_field(job, 'dates')
        company = _job_field(job, 'company')
        detail  = _extract_vessel_grt(job.get('bullets', []))
        # Falls back to role when no vessel/GRT could be extracted from
        # this entry's bullets — still distinct from Work Experience's
        # header (dates lead, no "—  Company | Dates" ordering repeated).
        headline = detail or _job_field(job, 'role')

        # Two experience_bullets entries for the same vessel/company/dates
        # (a real AI-rewrite artifact: one real assignment split into
        # separate entries by differing bullet content) are still two
        # distinct entries in Work Experience below -- but they're the
        # exact same row here with zero new information, so this section
        # (unlike Work Experience) skips the repeat rather than showing
        # an identical summary line twice.
        row_key = (dates, company, headline)
        if row_key in seen_rows:
            continue
        seen_rows.add(row_key)
        # A blank company/headline (real bug fixed, confirmed on real
        # output: Ankur Shrivastava's rewrite returned an empty
        # 'company' for every role) must not render as a dangling
        # "—   " with nothing after it — join only the parts actually
        # present rather than always including every slot.
        parts = [p for p in (dates, headline, company) if p]
        line = '   —   '.join(parts)
        add_body_text(doc, line, size=row_size, compact=compact)


def generate_docx(content: dict, job_id: str = 'output',
                  include_photo: bool = False, photo_bytes=None) -> str:
    """
    Main entry point.
    Takes rewritten content JSON from services/ai_client.py.
    Returns path to the generated DOCX file.

    Expected content keys:
    - name, contact (dict), summary, experience_bullets (list),
      skills (list), certifications (list), rank (str)

    include_photo/photo_bytes produce the photo-included variant (for
    direct human/recruiter-facing submission) at a distinct filename
    ({job_id}_ats_resume_photo.docx) alongside the existing photo-free
    default ({job_id}_ats_resume.docx, unchanged filename/behavior — the
    safer default for automated/bulk ATS upload channels, per Priority 2's
    research on embedded-image handling). Callers wanting both variants
    call this twice; see main.py/api.py.
    """
    # Enforce length limits before building
    rank = content.get('rank', '')
    content = enforce_length_limits(content, rank)
    compact = bool(content.get('_compact_mode'))

    doc = Document()

    # ── Page Setup (R13, R14) ─────────────────────────────────────────
    # 0.75in, not the previous 1in — confirmed via a real rendered page
    # (LibreOffice) that content well under the word-count budget
    # (~370 words, vs. a 600-word single-page target) was still spilling
    # a couple of lines onto a nearly-empty second page: cumulative
    # paragraph-spacing overhead across many short elements (a 10-line
    # Certifications list, a 5-line Documents section, etc.) isn't
    # captured by a word count at all, only by actual usable page area.
    # 0.75in is still a standard, readable professional-resume margin,
    # not a cramped one.
    for section in doc.sections:
        section.top_margin    = Inches(0.6)
        section.bottom_margin = Inches(0.6)
        section.left_margin   = Inches(0.8)
        section.right_margin  = Inches(0.8)
        section.header.is_linked_to_previous = False
        section.footer.is_linked_to_previous = False

    # Set default font
    style = doc.styles['Normal']
    style.font.name = 'Arial'
    style.font.size = Pt(10 if compact else 11)

    # ── CANDIDATE NAME (R16) ──────────────────────────────────────────
    name_p = doc.add_paragraph()
    name_run = name_p.add_run(_xml_safe(content.get('name', 'Candidate Name')))
    name_run.bold = True
    name_run.font.size = Pt(16 if compact else 18)
    name_run.font.name = 'Arial'
    name_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    name_p.paragraph_format.space_after = Pt(2)

    # ── RANK / TITLE — directly under the name ────────────────────────
    # Real, confirmed gap: 'rank' is populated on every rewrite (the
    # schema requires it, and api.py injects rewritten['rank'] = rank as
    # a fallback even when the rewrite fails) but was never actually
    # rendered anywhere in this file -- a recruiter's first-glance scan
    # of the document had no title/rank line at all under the candidate's
    # name, confirmed across multiple real candidates. Distinct from the
    # earlier fix for a blank job['role'] inside a Work Experience entry
    # (that one patches a rank the model failed to fill into an existing
    # rendered field; this is a rendered field that never existed at all).
    if content.get('rank'):
        rank_p = doc.add_paragraph()
        rank_run = rank_p.add_run(_xml_safe(str(content['rank']).title()))
        rank_run.font.size = Pt(12 if compact else 13)
        rank_run.font.name = 'Arial'
        rank_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        rank_p.paragraph_format.space_after = Pt(2)

    # ── CONTACT INFO — in body, never in header (R6, R16) ────────────
    contact = content.get('contact', {})
    contact_parts = []
    if contact.get('email'):    contact_parts.append(contact['email'])
    if contact.get('phone'):    contact_parts.append(contact['phone'])
    if contact.get('linkedin'): contact_parts.append(contact['linkedin'])
    if contact.get('github'):   contact_parts.append(contact['github'])

    contact_p = doc.add_paragraph()
    contact_run = contact_p.add_run(_xml_safe(' | '.join(contact_parts)))
    contact_run.font.size = Pt(10)
    contact_run.font.name = 'Arial'
    contact_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    contact_p.paragraph_format.space_after = Pt(4 if compact else 6)

    # ── CANDIDATE PHOTO (Priority 2) ──────────────────────────────────
    # Inline in the body near the header, never in header/footer/table/
    # text-box, never floating — see add_candidate_photo's docstring.
    # Only for the photo-included variant; gracefully skipped (not an
    # error) when the source had no extractable photo.
    if include_photo and photo_bytes:
        add_candidate_photo(doc, photo_bytes)

    # ── PROFESSIONAL SUMMARY (R19) ────────────────────────────────────
    if content.get('summary'):
        add_section_heading(doc, 'Professional Summary', compact=compact)
        add_body_text(doc, content['summary'], compact=compact)

    # ── CERTIFICATIONS (R23) ─────────────────────────────────────────
    # Placed immediately after the summary, ahead of Work Experience.
    # Maritime recruiters scan resumes in a strict priority order —
    # certificates first (the eligibility gate), then sea service, then
    # vessel type experience (see CLAUDE.md's Project Overview) — so the
    # #1 thing they check for shouldn't be buried mid-document. Section
    # detection in pipeline/scorer.py is presence-based, not
    # position-based, so this has no effect on the ATS score.
    certifications = _as_list(content.get('certifications', []))
    if certifications:
        add_section_heading(doc, 'Certifications', compact=compact)
        for cert in certifications:
            add_body_text(doc, str(cert), compact=compact)

    # ── SEA SERVICE RECORD (Priority 1.3) ─────────────────────────────
    add_sea_service_section(doc, content, compact=compact)

    # ── WORK EXPERIENCE (R21, R22) ────────────────────────────────────
    experience = _as_list(content.get('experience_bullets', []))
    if experience:
        add_section_heading(doc, 'Work Experience', compact=compact)
        for idx, job in enumerate(experience):
            if not isinstance(job, dict):
                continue
            # A blank company (real bug fixed, confirmed on real output:
            # Ankur Shrivastava's rewrite returned an empty 'company'
            # for every role) must not render as "Role  —    |  Dates"
            # with a dangling separator and nothing between it and the
            # next one — join only the parts actually present.
            head_parts = [p for p in (_job_field(job, 'role'), _job_field(job, 'company')) if p]
            role_line = '  —  '.join(head_parts)
            job_dates = _job_field(job, 'dates')
            if job_dates:
                role_line = f'{role_line}  |  {job_dates}' if role_line else job_dates
            role_p = add_body_text(doc, role_line, bold=True, compact=compact)
            # A full blank paragraph between roles costs a whole extra
            # line of vertical space on top of its own space_after —
            # confirmed as a real contributor to a thin resume spilling
            # onto a near-empty extra page. space_before on the NEXT
            # role's header gives the same visual separation for a
            # fraction of the height.
            if idx > 0:
                role_p.paragraph_format.space_before = Pt(5 if compact else 8)
            for bullet in job.get('bullets', []):
                add_bullet(doc, bullet, compact=compact)

    # ── SKILLS (R20) ──────────────────────────────────────────────────
    skills = _as_list(content.get('skills', []))
    if skills:
        add_section_heading(doc, 'Skills', compact=compact)
        skills_text = '  •  '.join(str(s) for s in skills)
        add_body_text(doc, skills_text, compact=compact)

    # ── EDUCATION ─────────────────────────────────────────────────────
    education = _as_list(content.get('education', []))
    if education:
        add_section_heading(doc, 'Education', compact=compact)
        for edu in education:
            add_body_text(doc, str(edu), compact=compact)

    # ── DOCUMENTS & LICENSE ─────────────────────────────────────────────
    documents = content.get('documents', {})
    if not isinstance(documents, dict):
        documents = {}
    if any(documents.values()):
        add_section_heading(doc, 'Documents & License', compact=compact)
        doc_items = []
        if documents.get('coc'):
            doc_items.append(f"Certificate of Competency: {documents['coc']}")
        if documents.get('cdc'):
            doc_items.append(f"CDC: {documents['cdc']}")
        if documents.get('indos'):
            doc_items.append(f"INDOS: {documents['indos']}")
        if documents.get('passport'):
            doc_items.append(f"Passport: {documents['passport']}")
        doc_items.extend(str(other) for other in _as_list(documents.get('other', [])))
        # Two short "Label: value" items per line -- these are typically
        # much shorter than a certification or bullet line, and one-per-
        # line was confirmed (real rendered page) to be a meaningful
        # contributor to thin content spilling onto a near-empty extra
        # page. A lone trailing item still gets its own line.
        for i in range(0, len(doc_items), 2):
            pair = doc_items[i:i + 2]
            add_body_text(doc, '   |   '.join(pair), compact=compact)

    # ── LANGUAGES ─────────────────────────────────────────────────────────
    languages = _as_list(content.get('languages', []))
    if languages:
        add_section_heading(doc, 'Languages', compact=compact)
        add_body_text(doc, '  •  '.join(str(l) for l in languages), compact=compact)

    # ── Save File ─────────────────────────────────────────────────────
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    suffix = '_photo' if include_photo else ''
    output_path = os.path.join(OUTPUT_DIR, f'{job_id}_ats_resume{suffix}.docx')
    doc.save(output_path)
    print(f'✓ DOCX saved: {output_path}')
    return output_path
