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
    'experience':       r'(?i)^\s*(experience|work experience|employment|work history|professional experience|sea service|seaservice|sailing experience|sea going experience|vessel experience|ship experience|service record|sea record)\s*$',
    'education':        r'(?i)^\s*(education|academic|qualification|academics|educational qualification|academic qualification)\s*$',
    'skills':           r'(?i)^\s*(skills|technical skills|competencies|technologies|core competencies|key skills|areas of expertise|expertise|skillset|technical expertise|professional skills)\s*$',
    'certifications':   r'(?i)^\s*(certifications?|licenses?|certificates?|documents?|document details|license details|certificate details|stcw certificates?|mandatory certificates?|courses?|training)\s*$',
    # ... existing ones stay ...
    'technical_expertise': r'(?i)^\s*(technical expertise|machinery|equipment|technical skills|key equipment|machinery expertise)\s*$',
    'documents':           r'(?i)^\s*(documents?|document details?|license details?|passport details?|seafarer documents?)\s*$',
    'license':             r'(?i)^\s*(licen[sc]e[s]?|coc details?|certificate of competency|meo class)\s*$',
    'languages':           r'(?i)^\s*(languages?|language proficiency|language skills)\s*$',
    'additional_experience': r'(?i)^\s*(additional experience|shore experience|pre-sea experience|other experience)\s*$',
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


def extract_sea_months_from_text(text: str) -> int:
    """
    Text-based fallback for sea service estimation when no PDF is available
    (e.g. re-scoring a generated DOCX). Parses MM/YYYY date ranges directly
    from experience text using the same pattern as the PDF-extraction
    fallback in extract_sea_months() above, minus the table/page handling.
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

    if not service_periods:
        # Last resort: any MM/YYYY pairs in the text
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