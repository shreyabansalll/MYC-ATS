# pipeline/maritime.py
import re
from pipeline import parser as parser_module
from rules.cert_rules import CERT_REQUIREMENTS, SEA_SERVICE_MIN
from rules.aliases import CERT_ALIASES

TANKER_TYPES    = ['chemical tanker', 'oil tanker', 'product tanker',
                   'crude oil tanker', 'lng', 'lpg', 'oil/chem tanker']
BULK_TYPES      = ['bulk carrier', 'ore carrier']
CONTAINER_TYPES = ['container vessel', 'container ship']
OFFSHORE_TYPES  = ['ahts', 'psv', 'osv', 'offshore']
PASSENGER_TYPES = ['passenger vessel', 'cruise', 'ro-ro', 'car carrier']


def cert_present(cert_key: str, raw_text: str) -> bool:
    aliases = CERT_ALIASES.get(cert_key, [cert_key.lower()])
    text_lower = raw_text.lower()
    return any(alias in text_lower for alias in aliases)


# Strips constructions like "coordinated with the Chief Engineer to
# implement..." or "...under the direction of the Chief Engineer to
# ensure..." before any rank pattern matching runs. Three confirmed real
# false positives share the identical shape -- a rank title used as the
# object of a sentence about interacting with a supervisor/colleague,
# preceded by "the " and followed by " to " (an infinitive describing
# what that OTHER person's role was for):
#   - Victor John: original source has "...working under the direction
#     of the Chief Engineer to ensure...". His actual rank is ETO, but
#     detect_rank returned 'chief engineer', which then fed the AI
#     rewrite prompt's "Current Sailing Rank" and corrupted the
#     generated Professional Summary itself: "Chief Engineer with 31
#     months of sea service..." for an actual ETO.
#   - Ankur Shrivastava: an AI-rewritten bullet, "Coordinated with the
#     Chief Engineer to implement PMS...", caused the same false
#     positive when Stage 7's re-score ran detect_rank against the
#     regenerated DOCX text.
#   - Gilfred Chettiar: original source has "Coordinated with the
#     Master and Chief Officer to maintain operational efficiency...",
#     a COMPOUND object (two ranks chained by "and" before "to") --
#     detect_rank returned 'chief officer' though he's never held that
#     role (his real progression: Deck Cadet -> Third Officer -> Second
#     Officer, and his cert is OOW, not a Chief Mate CoC), and this
#     wrong rank was likewise baked into his AI-generated Summary:
#     "Chief Officer with over 11 years of sea service...".
# A candidate describing their OWN rank in a seafarer resume essentially
# never takes this exact "the <rank[, rank...] and <rank>> to" shape --
# resumes state a rank as a title/header ("Chief Officer") or with
# "as"/"serving as", not "the Chief Officer to ...". Stripping the whole
# span first (rather than excluding each pattern string independently)
# means every variant of that rank -- full name and abbreviation alike
# -- correctly stops matching together, not just the one literal string
# that triggered the exclusion. The optional repeating group handles a
# chained list of other officers' titles (e.g. "the Master and Chief
# Officer to...") before the final rank and "to".
_RANK_TITLE_ALT = (
    r'chief officer|chief mate|second officer|third officer|'
    r'chief engineer|chief eng|second engineer|third engineer|'
    r'fourth engineer|electro technical officer|master|captain'
)
_THIRD_PARTY_RANK_MENTION = re.compile(
    rf'\bthe\s+(?:(?:{_RANK_TITLE_ALT})\s*(?:,\s*|\s+and\s+))*'
    rf'(?:{_RANK_TITLE_ALT})\s+to\b'
)


# Checked FIRST, before any full-document scan: a resume's own
# header/title line ("Jaideep Verma / Fourth Assistant Engineer",
# "VICTOR JOHN / Electro Technical Officer") is the candidate's own,
# authoritative self-declared rank. Confirmed via three real candidates
# (Victor John, Gilfred Chettiar, Jaideep Verma) that checking this
# first structurally dodges the ENTIRE family of "someone else's rank
# mentioned further down" false positives -- bullets describing a
# supervisor, a References section footer naming a referee's rank,
# possessive "Chief Engineer's guidance" phrasing -- in one general
# way, rather than requiring a fresh narrow regex for every new
# grammatical shape those mentions take. Confirmed real shapes NOT
# caught by _THIRD_PARTY_RANK_MENTION alone (found on Jagdish Nair and
# Abdullah Tauqir Khan, both real candidates whose actual rank is
# junior but whose detected rank was 'chief engineer'): "under Chief
# Engineer's guidance" (possessive, no "the"/"to") and "supporting the
# Chief Engineer and senior officers" (no trailing "to"). And Jaideep
# Verma's actual rank (Fourth Assistant Engineer, an entry-level
# trainee) was overridden by "Mr. Amit Kumar Lathar - Chief Engineer"
# in his References section -- a referee's title, not even a
# supervisor mentioned in a duty bullet. The full-document scan (with
# _THIRD_PARTY_RANK_MENTION still applied as defense-in-depth) remains
# the fallback for resumes whose header states no rank at all.
_HEADER_CHARS = 300


def detect_rank(text: str) -> str:
    rank_patterns = [
        # "<Rank> Deck Officer" phrasing (some CV templates say "Deck"
        # explicitly to disambiguate from the engine department, same
        # Chief/Second/Third numbering) -- confirmed real gap: Akshay
        # Bhardwaj's header states "Third Deck Officer" verbatim (his
        # real, current rank), which didn't match 'third officer' as a
        # substring (the word "Deck" sits between "Third" and
        # "Officer"). The header check found nothing there and fell
        # through to a "Deck Cadet" mention picked up from badly
        # column-bled Summary text (a US Visa expiry date bleeding
        # mid-sentence), misclassifying a Third Officer as the most
        # junior deck rank there is.
        ('chief officer',   ['chief officer', 'chief mate', 'c/o ', 'chief deck officer']),
        ('second officer',  ['second officer', '2nd officer', '2/o ', 'second deck officer']),
        ('third officer',   ['third officer', '3rd officer', '3/o ', 'third deck officer']),
        ('chief engineer',  ['chief engineer', 'c/e ', 'chief eng']),
        # US-convention "Assistant Engineer" titles map onto the Indian/
        # British Chief/Second/Third/Fourth numbering the rest of this
        # system uses -- both are 4-tier systems counting down from
        # Chief, just numbered differently (US doesn't count Chief as
        # "first"). Confirmed real gap: Abdullah Tauqir Khan's actual
        # rank, "Third Assistant Engineer", wasn't recognized by this
        # list at all (only by parser.py's separate, rarely-reached
        # RANK_KEYWORDS fallback) -- since the header-priority check
        # below only consults THIS list, his header match failed
        # silently and detect_rank fell through to a false "chief
        # engineer" match on a body-text supervisor mention instead.
        ('second engineer', ['second engineer', '2nd engineer', '2/e ',
                             'first assistant engineer']),
        ('third engineer',  ['third engineer', '3rd engineer', '3/e ',
                             'second assistant engineer']),
        ('fourth engineer', ['fourth engineer', '4th engineer', '4/e ',
                             'fourth assistant engineer', 'third assistant engineer']),
        ('eto',             ['electro technical officer', ' eto ', 'e.t.o']),
        ('bosun',           ['bosun', 'boatswain']),
        ('ab seaman',       ['able seaman', 'ab seaman', 'a.b. seaman']),
        ('motorman',        ['motorman']),
        ('fitter',          [' fitter']),
        ('oiler',           [' oiler']),
        ('deck cadet',      ['deck cadet', 'nautical cadet']),
        ('engine cadet',    ['engine cadet', 'engineering cadet']),
        ('jwko',            ['jwko', 'junior watchkeeping']),
        ('master',          ['master mariner', 'rank: master', 'position: master',
                             'sailing as master', "ship's captain", 'ship captain',
                             'vessel captain', 'sea captain']),
    ]

    header_lower = _THIRD_PARTY_RANK_MENTION.sub(' ', text[:_HEADER_CHARS].lower())
    for rank, patterns in rank_patterns:
        if any(p in header_lower for p in patterns):
            return rank

    text_lower = _THIRD_PARTY_RANK_MENTION.sub(' ', text.lower())
    for rank, patterns in rank_patterns:
        if any(p in text_lower for p in patterns):
            return rank
    try:
        return parser_module.infer_rank_fallback(text)
    except Exception:
        return 'unknown'


def detect_license_rank(text: str) -> str:
    text_lower = text.lower()
    if 'meo class i' in text_lower:
        return 'chief engineer'
    if 'meo class ii' in text_lower:
        return 'second engineer'
    if 'meo class iii' in text_lower or 'meo class 3' in text_lower:
        return 'third engineer'
    if 'meo class iv' in text_lower or 'meo class 4' in text_lower:
        return 'fourth engineer'
    if 'master mariner' in text_lower:
        return 'master'
    if 'oicnw' in text_lower:
        return 'officer of the watch'
    if 'eoow' in text_lower:
        return 'engineer officer of the watch'
    return ''


def extract_certificates(text: str) -> list:
    found = []
    for cert_key in CERT_ALIASES:
        if cert_present(cert_key, text):
            found.append(cert_key)
    extra_certs = [
        'BOSIET', 'HUET', 'OPITO', 'Advanced Firefighting',
        'Medical First Aid', 'Proficiency in Survival Craft',
        'Crowd Management', 'Crisis Management', 'ISPS',
        'High Voltage', 'DP', 'Dynamic Positioning', 'ECDIS',
        'Passenger Ship Familiarization', 'ARPA',
        'Tanker Familiarization', 'Chemical Tanker Certificate',
        'Oil Tanker Certificate', 'Gas Tanker Certificate',
        # Detection/display only -- NOT added to CERT_REQUIREMENTS, so
        # this cannot change any rank's cert-coverage score. Confirmed
        # via real MYC certification documentation as genuinely common,
        # currently entirely undetected certs/endorsements.
        'Ship Security Officer', 'SSO', 'Bridge Resource Management', 'BRM',
        'Bridge Team Management', 'BTM', 'Engine Room Resource Management', 'ERM',
        'Dangerous Cargo Endorsement', 'DCE', 'Certificate of Proficiency',
        'Rating Forming Part of a Navigational Watch', 'RFPNW',
        'Rating Forming Part of an Engine Room Watch', 'RFPEW',
    ]
    text_lower = text.lower()
    for cert in extra_certs:
        if cert.lower() in text_lower and cert not in found:
            found.append(cert)
    return found


def estimate_sea_service(pdf_path: str = None, text: str = None) -> int:
    """
    Tries PDF-table/PDF-text extraction first (richest source) when
    pdf_path actually points at a PDF. Falls back to regex date-range
    parsing against plain text otherwise — used when re-scoring a
    generated DOCX (no PDF at all), or when the original upload itself
    was a DOCX (nothing to extract from with a PDF reader).

    A successful PDF extraction is trusted even when it returns 0 — that's
    the correct, expected value for cadets (SEA_SERVICE_MIN['deck cadet']
    and ['engine cadet'] are both 0), not a signal that extraction failed.
    extract_sea_months() already swallows its own read/parse errors and
    returns 0 on failure (it never raises for a bad/non-PDF path), so a
    bare 0 alone can't distinguish "legitimately zero" from "couldn't even
    open this as a PDF" — checking the extension up front avoids ever
    needing to guess from the return value.
    """
    if pdf_path and pdf_path.lower().endswith('.pdf'):
        try:
            from pipeline import parser as p
            return int(p.extract_sea_months(pdf_path))
        except Exception:
            pass
    if text:
        from pipeline import parser as p
        try:
            return int(p.extract_sea_months_from_text(text))
        except Exception:
            return 0
    return 0


def detect_vessel_types(text: str) -> list:
    text_lower = text.lower()
    found = []
    all_types = (TANKER_TYPES + BULK_TYPES + CONTAINER_TYPES
                 + OFFSHORE_TYPES + PASSENGER_TYPES)
    for vtype in all_types:
        if vtype in text_lower:
            found.append(vtype)
    return list(dict.fromkeys(found))


def maritime_bonus_score(rank: str, sea_months: int, vessel_types: list,
                         matched: list, required_certs: list) -> int:
    bonus = 0
    min_months = SEA_SERVICE_MIN.get(rank, 6)
    if min_months > 0:
        if sea_months >= min_months * 3:   bonus += 8
        elif sea_months >= min_months * 2: bonus += 5
        elif sea_months >= min_months:     bonus += 2
    bonus += min(4, len(vessel_types))
    if required_certs and len(matched) == len(required_certs):
        bonus += 4
    if rank in ['master', 'chief engineer', 'chief officer', 'second engineer']:
        bonus += 4
    return min(20, bonus)


def maritime_score(raw_text: str, parsed: dict = None, pdf_path: str = None) -> dict:
    score        = 0
    issues       = []
    recommendations = []

    rank         = detect_rank(raw_text)
    license_rank = detect_license_rank(raw_text)
    certs        = extract_certificates(raw_text)
    experience_text = (parsed.get('sections', {}).get('experience', '') if parsed else '') or raw_text
    sea_months   = estimate_sea_service(pdf_path=pdf_path, text=experience_text)
    vessel_types = detect_vessel_types(raw_text)

    # 1. Certificate check (25pts)
    required_certs = CERT_REQUIREMENTS.get(rank, ['STCW', 'ENG1'])
    matched        = [r for r in required_certs if cert_present(r, raw_text)]
    missing_certs  = [r for r in required_certs if not cert_present(r, raw_text)]
    cert_pts       = int((len(matched) / max(len(required_certs), 1)) * 25)
    score += cert_pts
    if missing_certs:
        issues.append(f'MARITIME: Missing certificates for {rank.title()}: {missing_certs}')
        recommendations.append(f'Add missing certificates: {", ".join(missing_certs)}')

    # 2. Sea service check (20pts)
    min_months = SEA_SERVICE_MIN.get(rank, 6)
    if sea_months >= min_months:
        score += 20
    elif sea_months > 0:
        score += int((sea_months / max(min_months, 1)) * 20)
        issues.append(f'MARITIME: Sea service {sea_months}mo, minimum {min_months}mo for {rank.title()}')
        recommendations.append('Ensure all vessel contracts listed with exact sign-on/sign-off dates.')
    else:
        issues.append('MARITIME: Sea service duration not detected')
        recommendations.append('Add sea service records with sign-on/sign-off dates for each vessel.')

    # 3. Vessel type experience (10pts)
    if vessel_types:
        score += 10
    else:
        issues.append('MARITIME: No vessel types detected')
        recommendations.append('List vessel names with type and GRT/DWT for each contract.')

    # 4. Rank detection (10pts)
    if rank != 'unknown':
        score += 10
    else:
        issues.append('MARITIME: Rank/position not clearly stated')
        recommendations.append('State target rank clearly at top of resume.')

    # 5. Bonus scoring (0-20pts)
    bonus       = maritime_bonus_score(rank, sea_months, vessel_types, matched, required_certs)
    final_score = score + bonus

    return {
        'maritime_score':  final_score,
        'maritime_grade':  ('STRONG' if final_score >= 55
                            else 'ADEQUATE' if final_score >= 35
                            else 'WEAK'),
        'rank_detected':   rank,
        'license_rank':    license_rank,
        'certificates':    certs,
        'missing_certs':   missing_certs,
        'sea_months':      sea_months,
        'vessel_types':    vessel_types,
        'issues':          issues,
        'recommendations': recommendations,
        'cert_coverage':   f'{len(matched)}/{len(required_certs)}',
        'bonus_score':     bonus,
    }