# pipeline/parser.py
# STAGE 3 — Resume Parsing
# Turns raw text into structured JSON using Regex + spaCy NER
# Libraries: re (regex), spacy (NER), pdfplumber (PDF table extraction)

import re
import pdfplumber
from datetime import datetime
import spacy
from rules.aliases import RANK_KEYWORDS

# Load spaCy model once at startup
nlp = spacy.load('en_core_web_lg')

# Section header patterns — maps common headings to standard keys
# Maritime seafarer resumes use non-standard headings — these must all be covered
SECTION_HEADERS = {
    'summary':          r'(?i)^\s*(summary|professional summary|profile|objective|about me|career objective|personal profile)\s*$',
    # "Seafarer['']s Service Record[s]" is the standard heading for the
    # structured sign-on/sign-off table on real Indian seafarer resumes
    # (confirmed verbatim on two real candidates, Gilfred Chettiar and
    # Ankur Shrivastava) -- the bare "service record"/"sea record"
    # alternatives below never matched it, since split_sections() requires
    # a heading line to match a pattern IN FULL (^...$), and "Seafarer's
    # Service Record" isn't equal to "service record" as a whole line.
    # Real bug this caused: that table's content (now extracted cleanly
    # via pdfplumber's actual table grid, not word-coordinate guessing --
    # see extractor.py's _extract_real_tables) fell through to whatever
    # section preceded it in the raw text instead -- confirmed landing in
    # "skills" on both candidates -- so the AI rewrite prompt's Experience
    # field (which reads sections['experience'] specifically) never saw
    # this authoritative company/vessel/date data at all. The apostrophe
    # is optional and matches straight/curly/absent (source PDFs use a
    # curly ’ character, not the ASCII "'"), and "Records" may be
    # singular or plural.
    'experience':       r'(?i)^\s*(experience|work experience|employment|work history|professional experience|sea service|seaservice|sailing experience|sea going experience|vessel experience|ship experience|service record|sea record|seafarers?[\'’`]?s?\s+service\s+records?)\s*$',
    'education':        r'(?i)^\s*(education|academic|qualification|academics|educational qualification|academic qualification)\s*$',
    'skills':           r'(?i)^\s*(skills|technical skills|competencies|technologies|core competencies|key skills|areas of expertise|expertise|skillset|technical expertise|professional skills)\s*$',
    'certifications':   r'(?i)^\s*(certifications?|licenses?|certificates?|documents?|document details|license details|certificate details|stcw certificates?|mandatory certificates?|courses?|training)\s*$',
    # ... existing ones stay ...
    'technical_expertise': r'(?i)^\s*(technical expertise|machinery|equipment|technical skills|key equipment|machinery expertise)\s*$',
    'documents':           r'(?i)^\s*(documents?|document details?|license details?|passport details?|seafarer documents?)\s*$',
    'license':             r'(?i)^\s*(licen[sc]e[s]?|coc details?|certificate of competency|meo class)\s*$',
    'languages':           r'(?i)^\s*(languages?|language proficiency|language skills)\s*$',
    'additional_experience': r'(?i)^\s*(additional experience|shore experience|pre-sea experience|other experience)\s*$',
    # Real, general bug fixed: split_sections() below only tracks
    # RECOGNIZED headings -- any line under an unrecognized one gets
    # silently appended to whichever section came immediately before it.
    # None of these common resume sections were recognized at all, so
    # their content was bleeding into whatever real section preceded
    # them on the page (confirmed on a real resume: "Strengths" bled
    # into Certifications, "Hobbies"/"References" bled into Languages —
    # a referee's name and phone number ending up appended to the
    # candidate's own Languages section in generated output). This
    # entry doesn't need its content used anywhere downstream; its only
    # job is to give these headings somewhere of their OWN to land, so
    # they stop being silently absorbed into a section that matters.
    'other':               r'(?i)^\s*(strengths?|hobbies|interests?|references?|referees?|personal details?|declaration|achievements?|extra[- ]?curricular activities?|personal information)\s*$',
}


def parse_resume(raw_text: str) -> dict:
    doc = nlp(raw_text)
    return {
        'contact':  extract_contact(raw_text),
        'name':     extract_name(doc, raw_text),
        'sections': split_sections(raw_text),   # now includes tech_expertise, documents, license, languages
        'skills':   extract_skills(raw_text),
    }

def extract_contact(text: str) -> dict:
    email_pattern = r'\b([a-zA-Z][a-zA-Z0-9_.+-]{3,})@([a-zA-Z0-9-]+\.[a-zA-Z]{2,})\b'
    email = None
    for local, domain in re.findall(email_pattern, text):
        if not local.isdigit():
            email = f'{local}@{domain}'
            break

    # Find ALL phone numbers
    all_phones = re.findall(r'[+]?\d[\d\s\-().]{7,}\d', text)
    all_phones = [p.strip() for p in all_phones
                  if len(re.sub(r'\D', '', p)) >= 10]
    # Deduplicate
    seen = set()
    unique_phones = []
    for p in all_phones:
        digits = re.sub(r'\D', '', p)
        if digits not in seen:
            seen.add(digits)
            unique_phones.append(p)

    linkedin = re.search(r'linkedin\.com/in/[\w\-]+', text, re.IGNORECASE)
    github   = re.search(r'github\.com/[\w\-]+', text, re.IGNORECASE)

    return {
        'email':      email,
        'phone':      unique_phones[0] if unique_phones else None,
        'all_phones': unique_phones,            # ← new
        'linkedin':   linkedin.group() if linkedin else None,
        'github':     github.group()   if github   else None,
    }


def extract_name(doc, raw_text: str = '') -> str:
    BLACKLIST = {
        'yanmar','daihatsu','wartsila','wärtsilä','alfa laval','man b&w',
        'address','addres','personal','committ','rouse','donghua','donghwa',
        'kangrim','tanabe','cummins','saacke','aalborg','westfalia','mitsubishi',
        'unknown','experience','multitasking','seafarer','stro lea','ecdis',
        'crude oi','curde','medite','grt','m.v','mt','mv',
    }

    if raw_text:
        lines = [l.strip() for l in raw_text.split('\n') if l.strip()]
        if lines:
            first = re.sub(r'\s+', ' ', lines[0]).strip()
            first = re.split(r'\s*[-–]\s*', first)[0].strip()
            words = first.split()
            if 2 <= len(words) <= 4 and len(first) < 40:
                if not any(c.isdigit() for c in first):
                    if not any(c in first for c in ['@', ':', '/', '+', '|', '.']):
                        if not any(b in first.lower() for b in BLACKLIST):
                            return first

    # spaCy fallback: combine first two PERSON tokens to handle split-column names
    person_tokens = []
    for ent in doc.ents:
        if ent.label_ == 'PERSON':
            person_tokens.extend([token.text for token in ent if not token.is_punct])
            if len(person_tokens) >= 2:
                return ' '.join(person_tokens[:2])

    if person_tokens:
        return ' '.join(person_tokens[:2])

    return 'Unknown'


def infer_rank_fallback(raw_text: str) -> str:
    """Fallback rank inference using keyword matching."""
    text_lower = raw_text.lower()
    for rank, keywords in RANK_KEYWORDS.items():
        if any(kw in text_lower for kw in keywords):
            return rank
    return 'unknown'


_COURSE_DATE_ANCHOR = re.compile(r'\b\d{2}/\d{4}\b')


def _merge_course_card_lines(text: str) -> str:
    """
    Real, still-open problem this addresses: pipeline/extractor.py's
    column/line reconstruction produces one PDF line at a time, with no
    concept of a multi-line "card" -- a Courses table entry (course
    title, possibly wrapped across 2-3 lines; institute name, possibly
    wrapped across 2 lines; a bare reference number; a date) arrives as
    4-5 SEPARATE, DISCONNECTED lines. Confirmed on a real regenerated
    resume: with those lines disconnected, the AI rewrite had no way to
    tell "Centre For Maritime" and a nearby bare reference number
    belonged to a DIFFERENT card than the acronym it ended up pairing
    the number with -- it fabricated "(ECDIS) | 10502364112502510" by
    combining unrelated fragments that happened to be nearby in the
    text. This is a genuinely harder problem than the column-bleed and
    glued-word bugs fixed in extractor.py (see _column_bleed_penalty and
    _split_glued_words) -- true per-field row extraction needs real
    y-coordinate card clustering at the PDF-word level, not a text-level
    pass like this one.

    This is the tractable, lower-risk half of that fix: rather than
    parsing out which fragment is the title vs institute vs reference
    number (real NLP/layout work), it keeps a single course's fragments
    TOGETHER as one line instead of leaving them as separate orphan
    lines a downstream consumer can misassociate with a different
    card's fragments. Anchored on the MM/YYYY date, which this dataset's
    course-card layout places exactly once per card, always on the
    title's first line: every line from one date onward, up to (not
    including) the next date, is one course's fragments and gets joined
    with ' | '.

    Lines before the first date-anchor (the clean PASSPORT/CDC/document
    lines, and course-name-only lines with no date at all, both
    confirmed to appear above the dated Courses table on a real resume)
    are passed through completely unchanged, one per line, exactly as
    before this function existed.
    """
    lines = [l.strip() for l in text.split('\n') if l.strip()]

    merged = []
    current_chunk = []
    seen_first_date = False
    for line in lines:
        if _COURSE_DATE_ANCHOR.search(line):
            if current_chunk:
                merged.append(' | '.join(current_chunk))
            current_chunk = [line]
            seen_first_date = True
        elif seen_first_date:
            current_chunk.append(line)
        else:
            merged.append(line)
    if current_chunk:
        merged.append(' | '.join(current_chunk))

    return '\n'.join(merged)


def split_sections(text: str) -> dict:
    """
    Splits resume into sections by detecting standard header lines.
    Returns a dict: { 'experience': '...text...', 'education': '...text...' }
    """
    lines = text.split('\n')
    sections = {key: '' for key in SECTION_HEADERS}
    current_section = None

    for line in lines:
        matched = False
        for key, pattern in SECTION_HEADERS.items():
            if re.match(pattern, line.strip()):
                current_section = key
                matched = True
                break
        if not matched and current_section:
            sections[current_section] += line + '\n'

    if sections.get('certifications'):
        sections['certifications'] = _merge_course_card_lines(sections['certifications'])

    return sections


def extract_sea_months(pdf_path: str) -> int:
    """
    Extracts total sea service months from seafarer resumes.
    Handles two patterns found in MYC candidate resumes:
    1. Seafarer's Service Record table (DD/MM/YYYY sign-on/sign-off) — Page 2
    2. Work experience section with MM/YYYY date ranges — Page 1
    Merges overlapping periods and deduplicates.
    """
    service_periods = []

    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                # === PRIMARY: Service Record Table ===
                tables = page.extract_tables()
                for table in tables:
                    if not table or len(table) < 2:
                        continue
                    # Confirm this is a service record, not a contact/skills table
                    header_text = ' '.join(
                        str(c) for row in table[:2] for c in row if c
                    ).lower()
                    is_service = any(kw in header_text for kw in [
                        'sign on', 'signed on', 'sign off', 'vessel', 'rank', 'company'
                    ])
                    if not is_service:
                        continue

                    for row in table[1:]:
                        row_text = ' '.join(str(c) for c in row if c)
                        dates = re.findall(r'(\d{2}/\d{2}/\d{4})', row_text)
                        for i in range(0, len(dates) - 1, 2):
                            try:
                                d1 = datetime.strptime(dates[i], '%d/%m/%Y')
                                d2 = datetime.strptime(dates[i + 1], '%d/%m/%Y')
                                # Guard: must be realistic sea service dates
                                if (d2 > d1
                                        and 2005 <= d1.year <= 2026
                                        and 2005 <= d2.year <= 2026
                                        and (d2 - d1).days < 365 * 4):
                                    service_periods.append((d1, d2))
                            except Exception:
                                pass

                # === FALLBACK: MM/YYYY ranges in work experience text ===
                if not service_periods:
                    text = page.extract_text() or ''
                    for line in text.split('\n'):
                        # Skip metadata lines
                        if any(kw in line.lower() for kw in [
                            'passport', 'visa', 'dob', 'born', 'coc ', 'cdc ',
                            'indos', 'medical', 'expiry', 'valid till'
                        ]):
                            continue
                        ranges = re.findall(
                            r'(\d{2}/\d{4})\s*[-–]\s*(\d{2}/\d{4}|[Pp]resent|[Cc]urrent|[Dd]ate)',
                            line
                        )
                        for s, e in ranges:
                            try:
                                d1 = datetime.strptime(s, '%m/%Y')
                                d2 = (datetime.now() if any(x in e.lower()
                                      for x in ['present', 'current', 'date'])
                                      else datetime.strptime(e, '%m/%Y'))
                                if d2 > d1 and 2005 <= d1.year <= 2026:
                                    service_periods.append((d1, d2))
                            except Exception:
                                pass

                # === LAST RESORT: Find any year-year pairs in full page text ===
                if not service_periods:
                    all_mm_yyyy = re.findall(r'(\d{2}/\d{4})', text)
                    sea_dates = []
                    for d in all_mm_yyyy:
                        try:
                            dt = datetime.strptime(d, '%m/%Y')
                            if 2005 <= dt.year <= 2026:
                                sea_dates.append(dt)
                        except Exception:
                            pass
                    sea_dates.sort()
                    for i in range(0, len(sea_dates) - 1, 2):
                        d1, d2 = sea_dates[i], sea_dates[i + 1]
                        if d2 > d1 and (d2 - d1).days < 365 * 4:
                            service_periods.append((d1, d2))
    except Exception:
        return 0

    if not service_periods:
        return 0

    # Merge overlapping periods
    service_periods.sort(key=lambda x: x[0])
    merged = [list(service_periods[0])]
    for start, end in service_periods[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    total = sum((e - s).days // 30 for s, e in merged)
    return min(total, 400)


def _parse_flex_date(token: str):
    """
    Parses either MM/YYYY (the format the AI rewrite prompt requires) or
    YYYY-MM-DD (ISO, what the model actually returns often enough in
    practice to matter -- confirmed on real output: Ankur Shrivastava's
    experience_bullets dates came back as "2018-01-01 - 2020-12-31"
    despite the schema explicitly asking for "MM/YYYY - MM/YYYY"). A
    date-range regex hard-coded to MM/YYYY alone silently found zero
    matches against ISO dates, so extract_sea_months_from_text returned
    0 -- a real, harmful regression on a generated DOCX with sea service
    clearly present, exactly the "never silently zeroes out" failure
    mode this function exists to avoid (see CLAUDE.md's "Known-fixed
    bugs"). Returns None, not a raised exception, for an unrecognized
    shape -- callers already treat a parse failure as "skip this token".
    """
    for fmt in ('%m/%Y', '%Y-%m-%d'):
        try:
            return datetime.strptime(token, fmt)
        except ValueError:
            continue
    return None


_SEA_DATE_TOKEN = r'\d{2}/\d{4}|\d{4}-\d{2}-\d{2}'


def extract_sea_months_from_text(text: str) -> int:
    """
    Text-based fallback for sea service estimation when no PDF is available
    (e.g. re-scoring a generated DOCX). Parses MM/YYYY (and ISO YYYY-MM-DD,
    see _parse_flex_date) date ranges directly from experience text using
    the same pattern as the PDF-extraction fallback in extract_sea_months()
    above, minus the table/page handling.
    """
    if not text:
        return 0

    service_periods = []

    for line in text.split('\n'):
        if any(kw in line.lower() for kw in [
            'passport', 'visa', 'dob', 'born', 'coc ', 'cdc ',
            'indos', 'medical', 'expiry', 'valid till'
        ]):
            continue
        ranges = re.findall(
            rf'({_SEA_DATE_TOKEN})\s*[-–]\s*({_SEA_DATE_TOKEN}|[Pp]resent|[Cc]urrent|[Dd]ate)',
            line
        )
        for s, e in ranges:
            d1 = _parse_flex_date(s)
            if d1 is None:
                continue
            d2 = (datetime.now() if any(x in e.lower()
                  for x in ['present', 'current', 'date'])
                  else _parse_flex_date(e))
            if d2 is not None and d2 > d1 and 2005 <= d1.year <= 2026:
                service_periods.append((d1, d2))

    if not service_periods:
        # Last resort: any date tokens in the text
        all_tokens = re.findall(_SEA_DATE_TOKEN, text)
        sea_dates = []
        for d in all_tokens:
            dt = _parse_flex_date(d)
            if dt is not None and 2005 <= dt.year <= 2026:
                sea_dates.append(dt)
        sea_dates.sort()
        for i in range(0, len(sea_dates) - 1, 2):
            d1, d2 = sea_dates[i], sea_dates[i + 1]
            if d2 > d1 and (d2 - d1).days < 365 * 4:
                service_periods.append((d1, d2))

    if not service_periods:
        return 0

    service_periods.sort(key=lambda x: x[0])
    merged = [list(service_periods[0])]
    for start, end in service_periods[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    total = sum((e - s).days // 30 for s, e in merged)
    return min(total, 400)


def extract_skills(text: str) -> list:
    """
    Matches resume text against a hardcoded skills list.
    Later this will be replaced with a full 10,000-term skills corpus.
    Case-insensitive matching.
    """
    KNOWN_SKILLS = [
    # Maritime Certificates
    'STCW', 'Standards of Training Certification and Watchkeeping',
    'CoC', 'Certificate of Competency', 'GMDSS', 'ENG1',
    'BOSIET', 'HUET', 'OPITO', 'CBSP',
    'Basic Safety Training', 'BST',
    'Advanced Firefighting', 'Medical First Aid',
    'Proficiency in Survival Craft', 'PSC',
    'Crowd Management', 'Crisis Management',
    'Security Awareness', 'ISPS',
    'High Voltage', 'DP', 'Dynamic Positioning',
    'Tanker Familiarization', 'Chemical Tanker',
    'Oil Tanker', 'Liquefied Gas Tanker',
    'ECDIS', 'Electronic Chart Display',
    'ARPA', 'Radar', 'AIS',
    'Passenger Ship Familiarization',

    # Regulations & Codes
    'COLREGS', 'COLREGs', 'MARPOL', 'SOLAS', 'ISM',
    'ISM Code', 'ISPS Code', 'MLC', 'Maritime Labour Convention',
    'SMS', 'Safety Management System',
    'ISGOTT', 'COSWP', 'IMO',

    # Ranks & Roles
    'Master', 'Chief Officer', 'Second Officer', '2nd Officer',
    'Third Officer', '3rd Officer', 'Deck Cadet',
    'Chief Engineer', 'Second Engineer', '2nd Engineer',
    'Third Engineer', '3rd Engineer', 'Fourth Engineer',
    'ETO', 'Electro Technical Officer',
    'Bosun', 'AB Seaman', 'Able Seaman',
    'Motorman', 'Fitter', 'Oiler',
    'JWKO', 'Junior Watchkeeping Officer',

    # Vessel Types
    'Chemical Tanker', 'Oil Tanker', 'Product Tanker',
    'Crude Oil Tanker', 'LNG', 'LPG',
    'Bulk Carrier', 'Container Vessel', 'Container Ship',
    'Ro-Ro', 'Car Carrier', 'Passenger Vessel',
    'Offshore', 'AHTS', 'PSV', 'OSV',
    'General Cargo', 'Reefer',

    # Navigation & Operations
    'Watchkeeping', 'Bridge Watch', 'Engine Watch',
    'Passage Planning', 'Chart Correction',
    'Cargo Operations', 'Ballast Management',
    'Mooring', 'Anchoring', 'Stability',
    'Planned Maintenance System', 'PMS',
    'Bunkering', 'Tank Cleaning',

    # Soft Skills
    'Leadership', 'Communication', 'Teamwork',
    'Problem Solving', 'Safety Culture',
    'Crisis Management', 'Decision Making',
]
    text_lower = text.lower()
    found = [skill for skill in KNOWN_SKILLS if skill.lower() in text_lower]
    return found