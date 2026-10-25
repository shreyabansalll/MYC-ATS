# services/ai_client.py
import json
import httpx
import re
import time
import logging
from types import SimpleNamespace

import anthropic
from anthropic import (
    APIConnectionError, APITimeoutError, APIStatusError,
    AuthenticationError, RateLimitError, InternalServerError,
)
from config import ANTHROPIC_API_KEY, CLAUDE_MODEL
from generic_ats.prompts import (
    CRITICAL_TRUTHFULNESS_RULES, GENERIC_IMPROVE_PROMPT, STRICT_RETRY_ADDENDUM,
)
from generic_ats.keywords import extract_job_keywords
from generic_ats.writing_quality import post_process_rewrite_result, sanitize_prompt_input
from rules.cruise_line_keywords import (
    CRUISE_LINE_KEYWORDS, UNIVERSAL_CRUISE_KEYWORDS, resolve_cruise_line,
)

log = logging.getLogger(__name__)

if not ANTHROPIC_API_KEY:
    log.warning('"ANTHROPIC_API_KEY not set — AI rewrite will fail for every job"')

def _build_claude_client():
    """
    Create an Anthropic client without crashing when ambient proxy config
    or API setup is problematic.

    Always constructs with an explicit httpx.Client(trust_env=False), so
    HTTP_PROXY/HTTPS_PROXY/ALL_PROXY never affect this client.
    """
    if not ANTHROPIC_API_KEY:
        return None

    try:
        return anthropic.Anthropic(
            api_key=ANTHROPIC_API_KEY,
            http_client=httpx.Client(trust_env=False),
        )
    except Exception as e:
        log.error('Failed to construct Claude client: %s', e, exc_info=True)
        return None


client = _build_claude_client()

LLM_TRANSIENT_ERRORS = (
    APIConnectionError, APITimeoutError, RateLimitError, InternalServerError,
)
LLM_RETRY_BACKOFF_SECONDS = (1, 2, 4)


def _is_retryable_status(e: Exception) -> bool:
    if isinstance(e, AuthenticationError):
        return False
    if not isinstance(e, APIStatusError):
        return False
    return e.status_code in (408, 409, 429, 500, 502, 503, 529)

# Permanent by design, not a stopgap — seafarers submit one resume to many
# manning agencies/vessels rather than tailoring per posting, so there is no
# per-candidate JD to ever collect. Do not "fix" this into a JD-upload
# feature; target_rank + rank-detection is the complete, intended feature.
MARITIME_JDS = {
    'deck_officer': """
        Seeking experienced Deck Officer with valid STCW certification and CoC.
        Required: GMDSS, ENG1 Medical, COLREGS knowledge, ECDIS certification.
        Experience with tankers, bulk carriers or container vessels preferred.
        Strong knowledge of ISM Code, MARPOL, SOLAS regulations required.
        Minimum 12 months sea service. Leadership and communication skills essential.
    """,
    'chief_officer': """
        Chief Officer required for deep sea vessel operations.
        Valid Chief Mate CoC essential. Minimum 24 months sea service.
        Required: STCW, GMDSS GOC, ENG1, Advanced Firefighting, Medical First Aid,
        Crowd Management, Crisis Management. Cargo ops, stability, ISM/ISPS compliance.
    """,
    'second_officer': """
        Second Officer required for navigational watch duties on commercial vessel.
        Valid Second Mate CoC required. STCW mandatory. GMDSS GOC essential.
        ENG1 Medical required. ECDIS, radar, passage planning, chart correction.
        Minimum 12 months sea service. COLREGS knowledge essential.
    """,
    'engineer': """
        Marine Engineer required for engine room operations.
        Valid Engineer Officer CoC required. STCW certification mandatory.
        Experience with main propulsion, auxiliary machinery, fuel management.
        MARPOL, planned maintenance systems (PMS). ENG1 Medical required.
    """,
    'fourth_engineer': """
        Fourth Engineer required for engine room operations.
        Valid STCW and CoC required. ENG1 Medical essential.
        Auxiliary machinery, fuel oil systems, bilge operations experience.
        MARPOL, ISM Code, planned maintenance systems (PMS) knowledge.
    """,
    'eto': """
        Electro Technical Officer required. Valid ETO CoC and STCW mandatory.
        ENG1 Medical essential. GMDSS equipment, ECDIS, AIS, VDR maintenance.
        High voltage systems knowledge. Automation and PLC experience preferred.
        ISM Code, Planned Maintenance System (PMS) experience required.
    """,
    'able_seaman': """
        Able Seaman required for deck operations. STCW Basic Safety mandatory.
        ENG1 Medical required. Mooring, anchoring, cargo handling experience.
        Safety procedures, firefighting, and survival techniques knowledge.
    """,
    'oiler': """
        Oiler required for engine room operations. STCW Basic Safety mandatory.
        ENG1 Medical required. Engine room watchkeeping, fuel oil transfer, bilge ops.
        Lubrication systems, pump operations, valve maintenance experience.
    """,
    'deck_cadet': """
        Deck Cadet required. STCW Basic Safety mandatory. ENG1 Medical required.
        Basic navigation and seamanship. Mooring and deck operations.
        Willingness to learn under senior officer supervision.
    """,
    'rating': """
        Rating required. STCW Basic Safety certificate required. ENG1 mandatory.
        Good teamwork and communication skills. Previous sea service preferred.
    """,
}

# Keys the rewrite JSON schema (see build_rewrite_prompt's RETURN ONLY
# this JSON block) always requires — used to detect a response that
# parsed successfully as JSON but was actually cut off mid-generation
# (hit max_tokens) at a point that happened to land on an earlier
# closing brace. That produces syntactically valid but INCOMPLETE JSON,
# silently missing every field after the truncation point — a real
# content-loss mechanism that a bare json.loads() success can't catch on
# its own, since it isn't a parse error.
REQUIRED_REWRITE_KEYS = {
    'summary', 'experience_bullets', 'skills',
    'certifications', 'education', 'documents', 'languages',
}

# Field-name drift seen in real LLM responses despite the
# prompt's explicit schema: the model renames a required key to a
# synonym/camelCase variant instead of using the schema's exact name.
# Keyed by the drifted name actually observed, mapped to the real key.
_REWRITE_KEY_ALIASES = {
    'experience':        'experience_bullets',
    'technicalExpertise': 'technical_expertise',
}

# Same drift, one level down: an experience_bullets entry using 'rank'
# for the job title (ambiguous with the candidate's own top-level rank),
# 'responsibilities' instead of 'bullets', and separate startDate/endDate
# instead of a single 'dates' string. 'job_title'/'key_responsibilities'
# confirmed on real LLM model output.
_EXPERIENCE_ENTRY_KEY_ALIASES = {
    'rank':               'role',
    'job_title':          'role',
    'responsibilities':   'bullets',
    'key_responsibilities': 'bullets',
}

# Real drift observed with various models: unlike
# occasional single-key wrapper drift, models can routinely
# invent their own entirely different top-level key names for the SAME
# required field -- and not even a stable one call to call ("skills" has
# come back as "core_competencies", "technical_skills", AND
# "technical_skills_and_competencies" across otherwise-identical real
# requests). Substring matching against a real required key's name
# (rather than a fixed list of exact alternate spellings) is what
# actually generalizes across that call-to-call variation -- see
# _fuzzy_match_top_level_keys(), which uses this table.
_FUZZY_TOP_LEVEL_KEY_PATTERNS = [
    ('summary',            ('summary',)),
    ('experience_bullets', ('experience', 'employment_history', 'work_history')),
    ('skills',             ('competenc', 'technical_skill', 'core_skill')),
    ('certifications',     ('certif', 'training_and_cert', 'certs_and_training')),
    ('education',          ('education', 'academic_qualif', 'qualifications')),
    ('documents',          ('document', 'license_and_doc')),
    ('languages',          ('language',)),
]

# 'documents' and 'languages' were the original two REQUIRED_REWRITE_KEYS
# entries that generate_docx() and api.py's _validate_rewrite_output()
# already treat as fully optional -- an absent/empty 'documents' dict or
# 'languages' list just means that section is skipped in the generated
# DOCX, not a data-loss bug. So when a response omits them outright but
# finish_reason confirms it wasn't cut off mid-generation, default them
# instead of failing the whole rewrite over two cosmetic sections.
#
# 'education' joined this set when testing varying model outputs:
# confirmed on real, repeated calls (retry attempts for the same
# candidate, with finish_reason == 'stop', i.e. NOT truncated) that some
# models simply omit "education" from their JSON -- treating that omission as a truncation signal
# (education's original, deliberate design) now means real jobs exhaust
# every retry and fall back to "AI rewrite unavailable" over a single
# missing but genuinely non-critical field. generate_docx() already
# handles an empty education list exactly like documents/languages (a
# simple _as_list() with no non-empty assumption -- see
# pipeline/generator.py), so this is safe the same way those two already
# were. Real content isn't lost by this alone: enforce_education_
# completeness() (mirroring enforce_language_completeness) restores any
# real source education lines the model dropped, the same restore-don't-
# invent approach already used for certifications and languages.
_DEFAULTABLE_REWRITE_KEYS = {
    'documents': {},
    'languages': [],
    'education': [],
}


def _stringify_cert(cert):
    """
    Coerces a certification entry that arrived as a dict (schema drift --
    e.g. {"type": "CDC", "number": "...", "expirationDate": "..."}
    instead of the prompt's flat '[Name] | [Authority] | [Year]' string)
    into a plain string. enforce_minimum_quality's cert.replace() calls,
    generate_docx, and _validate_rewrite_output's substring checks all
    assume a string; this joins whatever fields are present rather than
    inventing or dropping data.

    Passport/visa/CDC/INDOS numbers the model mis-files under
    "certifications" (they belong in "documents" per build_rewrite_
    prompt's Rule 6) are moved there separately by
    _reclassify_document_like_certs, called right after this in
    _normalize_rewrite_result -- this function's only job is producing a
    plain string, not categorization.

    Known key names ('type'/'number'/... ) aren't exhaustive -- confirmed
    on real output that the model also returns {"name": "..."} for a
    plain course/cert title, no other fields at all. The fallback for an
    unrecognized dict shape used to be str(cert) -- a literal Python
    dict repr like "{'name': 'High Voltage Training Certificate'}"
    landing in the candidate's visible output, confirmed as a real bug.
    Falls back to joining every string/number value present in the dict
    (in whatever order the dict provides, typically insertion order)
    rather than a fixed key list, so an unanticipated key name still
    produces readable text instead of a repr dump.
    """
    if isinstance(cert, dict):
        parts = [str(cert[k]) for k in ('type', 'number', 'expirationDate', 'issueDate', 'name', 'title') if cert.get(k)]
        if parts:
            return ' | '.join(parts)
        parts = [str(v) for v in cert.values() if isinstance(v, (str, int, float)) and str(v).strip()]
        return ' | '.join(parts) if parts else ''
    return cert


def _drop_fabricated_acronym_number_pairs(certifications: list) -> list:
    """
    Deterministic safety net, not a prompt instruction alone -- an LLM
    cannot be trusted to reliably stop this by wording alone (the prompt
    now explicitly forbids it too, but this catches it regardless, and
    confirmed necessary: the exact fabricated wording is genuinely
    nondeterministic call to call, not one fixed shape a narrower regex
    could reliably match).

    A real reference number belongs to exactly one course/cert card.
    Confirmed on real output across multiple live calls that, given a
    messy source Courses table, the model sometimes attaches the SAME
    reference number to a second, shorter, less-descriptive entry as
    well -- observed shapes have included '(ECDIS) | 10502364112502510',
    '(ECDIS) Course | 10502364112502510', and even a real-looking title
    paired with a number that demonstrably belongs to a DIFFERENT
    course's card elsewhere in the same list ('Medical First Aid |
    <a different course's number>'). Rather than match one fixed shape,
    this generalizes: whenever the same 10+-digit reference number
    appears in more than one certification entry, only the single most
    descriptive (longest) entry is kept -- confirmed on real examples
    that the genuine course-card entry (title + institute + number,
    reconstructed from parser.py's _merge_course_card_lines) is
    consistently longer than a fabricated one (the model has nothing
    but the bare number to elaborate a fabrication from, so it can't
    produce as much real content as an entry actually grounded in
    source). A number appearing in only one entry is left untouched --
    nothing to compare it against, so no basis to drop it.
    """
    number_to_indices = {}
    for i, cert in enumerate(certifications):
        for num in set(re.findall(r'\d{10,}', str(cert))):
            number_to_indices.setdefault(num, []).append(i)

    drop_indices = set()
    for num, indices in number_to_indices.items():
        if len(indices) <= 1:
            continue
        indices_by_length = sorted(indices, key=lambda i: len(str(certifications[i])), reverse=True)
        drop_indices.update(indices_by_length[1:])

    return [c for i, c in enumerate(certifications) if i not in drop_indices]


def _drop_fabricated_reference_numbers(certifications: list, raw_text: str) -> list:
    """
    Strips a certification's reference/serial number when that exact
    number does not appear anywhere in the source resume text at all --
    the model has no ground truth for a number beyond source, so one
    that isn't there can only be invented. Confirmed on real output
    (Ankur Shrivastava): two separate "Certificate of Competency (CoC)"
    entries with numbers "123456" and "789012", neither of which appears
    anywhere in the source PDF -- classic sequential placeholder-number
    fabrication. Distinct from _drop_fabricated_acronym_number_pairs
    (which only catches the SAME number reused across multiple entries):
    here each entry has its own invented number, so that check never
    fires. Strips the number rather than the whole entry -- the cert
    NAME may still be genuine (this candidate's License section does
    show a real CoC), only the fabricated number is unsupported. Entries
    left identical after stripping (both invented numbers attached to
    the same real cert name) are deduped rather than shown as repeats.
    """
    if not raw_text:
        return certifications
    cleaned = []
    seen = set()
    for cert in certifications:
        cert_str = str(cert)
        for num in set(re.findall(r'\d{4,}', cert_str)):
            if num not in raw_text:
                cert_str = re.sub(r'[|\-:]?\s*\b' + re.escape(num) + r'\b\s*', ' ', cert_str)
        cert_str = re.sub(r'\s+', ' ', cert_str).strip(' |-')
        if not cert_str or cert_str.lower() in seen:
            continue
        seen.add(cert_str.lower())
        cleaned.append(cert_str)
    return cleaned


def _stringify_education(edu):
    """Same coercion as _stringify_cert, for education entries returned as dicts."""
    if isinstance(edu, dict):
        parts = [str(edu[k]) for k in ('degree', 'institution', 'graduationDate') if edu.get(k)]
        return ' — '.join(parts) if parts else str(edu)
    return edu


def _stringify_summary(value):
    """
    Coerces the top-level 'summary' field to a plain string. Real crash
    fixed, confirmed on a real live run: enforce_minimum_quality calls
    split_into_sentences(summary), which calls text.replace(...) with no
    type guard -- a 'summary' that arrived as a dict (schema drift, same
    category certifications/education already needed this for) crashed
    with "'dict' object has no attribute 'replace'", losing an
    otherwise-successful rewrite entirely. Joins whatever text is present
    rather than inventing or dropping content.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        parts = [str(v) for v in value.values() if isinstance(v, (str, int, float)) and str(v).strip()]
        return ' '.join(parts)
    if isinstance(value, list):
        return ' '.join(str(v) for v in value)
    return str(value) if value is not None else ''


def _normalize_experience_entry(entry):
    """
    Normalizes a single experience_bullets entry against the same
    schema drift _normalize_rewrite_result fixes at the top level (see
    _EXPERIENCE_ENTRY_KEY_ALIASES), plus vessel name/GRT given as
    separate fields instead of folded into the bullet text the way
    Rule 2 (VESSEL DETAILS MANDATORY) requires. Without the vessel-detail
    fold-in, an entry can pass the top-level required-keys check yet
    render with no vessel name/GRT anywhere in its bullets -- silently
    worse than today's honest failure-with-fallback, since maritime
    scoring depends on that text.
    """
    if not isinstance(entry, dict):
        return entry

    for drifted_name, real_name in _EXPERIENCE_ENTRY_KEY_ALIASES.items():
        if drifted_name in entry and real_name not in entry:
            entry[real_name] = entry.pop(drifted_name)

    if 'dates' not in entry and ('startDate' in entry or 'endDate' in entry):
        entry['dates'] = f"{entry.get('startDate', '')} - {entry.get('endDate', '')}".strip(' -')
    # snake_case variant confirmed in a legacy model response -- a
    # bare 'start_date' with no end date/'Present' marker at all in some
    # responses (an ongoing role), not just a differently-cased key name.
    if 'dates' not in entry and ('start_date' in entry or 'end_date' in entry):
        entry['dates'] = f"{entry.get('start_date', '')} - {entry.get('end_date', 'Present')}".strip(' -')

    # 'summary' as a single duty-description STRING instead of a
    # 'bullets' LIST -- confirmed on real output. Unlike the rank/
    # responsibilities key-renames above, this needs type conversion,
    # not just a key rename: generate_docx and enforce_length_limits'
    # bullets[:3] slicing both assume 'bullets' is a list -- a string
    # left in that key would silently slice to its first 3 CHARACTERS,
    # not its first 3 sentences, and generate_docx's `for bullet in
    # job.get('bullets', [])` would iterate character by character.
    # Confirmed as a real content-loss risk: without this, the entire
    # duty description for that role renders as nothing at all (no
    # 'bullets' key present) or as single characters (a string left in
    # place of a list).
    if not isinstance(entry.get('bullets'), list) or not entry.get('bullets'):
        duty_text = entry.get('summary')
        if isinstance(duty_text, str) and duty_text.strip():
            entry['bullets'] = split_into_sentences(duty_text)[:3]
        elif not isinstance(entry.get('bullets'), list):
            entry['bullets'] = []

    vessel_name = entry.get('vesselName')
    vessel_grt  = entry.get('vesselGRT')
    bullets     = entry.get('bullets')
    if vessel_name and isinstance(bullets, list):
        already_mentioned = any(str(vessel_name) in str(b) for b in bullets)
        if not already_mentioned:
            grt_part = f' (GRT {vessel_grt})' if vessel_grt else ''
            bullets.insert(0, f'Served aboard {vessel_name}{grt_part}.')

    # Plural variant confirmed in a legacy model response: a
    # separate list of vessel names, with NONE of them actually
    # mentioned anywhere in "key_responsibilities"/"bullets" -- real,
    # confirmed content loss otherwise, since Rule 2 (VESSEL DETAILS
    # MANDATORY) is exactly what this data satisfies and generate_docx
    # only ever reads "bullets". Matched by substring rather than one
    # exact key name: confirmed unstable call to call even for the SAME
    # candidate across otherwise-identical requests -- seen as both
    # "vessels_served" and, in a separate real call, plain "vessels".
    # Deliberately excludes "vesselName"/"vesselGRT" (singular, handled
    # above) so this doesn't double-process them as a 9-character list.
    if isinstance(bullets, list):
        for key, value in list(entry.items()):
            if key in ('vesselName', 'vesselGRT'):
                continue
            if 'vessel' not in key.lower():
                continue
            if not isinstance(value, list) or not value:
                continue
            already_mentioned = any(
                any(str(v) in str(b) for b in bullets) for v in value
            )
            if not already_mentioned:
                bullets.insert(0, f'Served aboard {", ".join(str(v) for v in value)}.')

    return entry


def _fuzzy_match_top_level_keys(result: dict) -> dict:
    """
    Renames top-level keys that are STRING- or LIST-valued (a plain
    dict-merge, as the wrapper-unwrap step below already does, only
    helps for dict-valued keys) using substring matching against
    _FUZZY_TOP_LEVEL_KEY_PATTERNS, when the real required key is still
    absent. Runs before the wrapper-unwrap/exact-alias steps.

    Confirmed necessary when handling real-world LLM variations: unlike
    an exact, stable set of alternate names
    (which _REWRITE_KEY_ALIASES covers), models' alternate names
    for the SAME field can vary call to call ("skills" observed as
    "core_competencies", "technical_skills", and
    "technical_skills_and_competencies" across otherwise-identical real
    requests for different candidates) -- no fixed alias table can track
    that, but every observed variant does contain a recognizable
    substring of the real key's meaning.
    """
    if not isinstance(result, dict):
        return result
    used_keys = set()
    for real_key, patterns in _FUZZY_TOP_LEVEL_KEY_PATTERNS:
        if real_key in result:
            continue
        for key in result:
            if key in used_keys or key == real_key:
                continue
            key_lower = key.lower()
            if any(p in key_lower for p in patterns):
                result[real_key] = result.pop(key)
                used_keys.add(real_key)
                break
    return result


def _normalize_rewrite_result(result, finish_reason) -> dict:
    """
    Recovers from schema drift before the required-keys check runs, so a
    genuinely complete response isn't misreported as truncated:

    0. Fuzzy top-level renames for string/list-valued drifted keys (see
       _fuzzy_match_top_level_keys) -- must run before step 1 below,
       since that step only merges DICT-valued unknown keys.
    1. Wrapper-key drift, full AND partial: a valid response always has
       every REQUIRED_REWRITE_KEYS entry at the top level, so a dict-
       valued top-level key that isn't itself one of the schema's known
       field names can only be an invented wrapper (whatever it's named,
       e.g. "candidate") -- merge its contents up into the top level.
       Handles both the case seen first (the ENTIRE payload nested under
       one wrapper key) and a second, real shape confirmed later
       (only SOME fields -- e.g. "skills" -- nested under "candidate",
       while "summary"/"experience"/"education"/"certifications" are
       already top-level siblings in the SAME response). The old check
       (unwrap only when there's exactly one top-level key) missed this
       second shape entirely, since the response has several top-level
       keys -- just not all of them.
    2. Field-name drift: known renames (_REWRITE_KEY_ALIASES) are mapped
       back to the schema's real key, at both the top level and within
       each experience_bullets entry (_normalize_experience_entry).
    3. Non-string certification/education entries are flattened (see
       _stringify_cert / _stringify_education) so downstream code that
       assumes strings doesn't crash on them, then any entry that's
       actually a document (passport/visa/CDC/INDOS/yellow fever) is
       moved out of "certifications" into "documents" (see
       _reclassify_document_like_certs) so the rendered Certifications
       section isn't cluttered with document numbers.
    4. 'documents'/'languages' are defaulted to their empty value if
       still missing and finish_reason != 'length' -- see
       _DEFAULTABLE_REWRITE_KEYS. Left alone (not defaulted) when
       finish_reason == 'length', since real truncation lands at the
       schema's tail and would produce exactly these two as "missing" --
       defaulting them there would mask a genuine cutoff.
    """
    if not isinstance(result, dict):
        return result

    result = _fuzzy_match_top_level_keys(result)

    known_top_level = (
        REQUIRED_REWRITE_KEYS | set(_DEFAULTABLE_REWRITE_KEYS)
        | set(_REWRITE_KEY_ALIASES) | {'rank', 'technical_expertise', 'issues_fixed'}
    )
    for key in list(result.keys()):
        if key in known_top_level:
            continue
        value = result[key]
        if isinstance(value, dict):
            for inner_key, inner_value in value.items():
                if inner_key not in result:
                    result[inner_key] = inner_value
            del result[key]

    for drifted_name, real_name in _REWRITE_KEY_ALIASES.items():
        if drifted_name in result and real_name not in result:
            result[real_name] = result.pop(drifted_name)

    if isinstance(result.get('experience_bullets'), list):
        # Defense-in-depth: a plain string entry (instead of a dict) in
        # this list would otherwise survive _normalize_experience_entry
        # untouched (it already returns non-dict entries as-is) and crash
        # the first downstream .get() call on it -- confirmed as a real,
        # not just theoretical, risk: enforce_minimum_quality's
        # has_real_exp check crashed with exactly "'str' object has no
        # attribute 'get'" on unnormalized LLM output
        # before that specific check was guarded. Converts a bare string
        # into a minimal real entry (empty role/company/dates, the
        # string itself as its one bullet) rather than dropping it --
        # enforce_minimum_quality's existing role-backfill (further
        # below) fills the empty role in from the candidate's rank.
        result['experience_bullets'] = [
            e if isinstance(e, dict) else {'role': '', 'company': '', 'dates': '', 'bullets': [str(e)]}
            for e in result['experience_bullets']
        ]
        result['experience_bullets'] = [_normalize_experience_entry(e) for e in result['experience_bullets']]

    if isinstance(result.get('certifications'), list):
        result['certifications'] = [_stringify_cert(c) for c in result['certifications']]
        # Real bug fixed, confirmed on real output: the model extracted a
        # Courses table's bare reference numbers ("10502363112502312")
        # as standalone certification entries, IN ADDITION to using them
        # correctly inside a full course-card entry elsewhere in the
        # same list. A bare number with no letters at all carries zero
        # identifiable information on its own -- drop it rather than
        # keep it, since the real information (title + institute +
        # number together) is already present in the other entry.
        result['certifications'] = [
            c for c in result['certifications']
            if str(c).strip() and not re.fullmatch(r'[\d\s\-]+', str(c).strip())
        ]
        result['certifications'] = _drop_fabricated_acronym_number_pairs(result['certifications'])

    result = _reclassify_document_like_certs(result)

    if isinstance(result.get('education'), list):
        result['education'] = [_stringify_education(e) for e in result['education']]

    if 'summary' in result and not isinstance(result['summary'], str):
        result['summary'] = _stringify_summary(result['summary'])

    if finish_reason != 'length':
        for key, default in _DEFAULTABLE_REWRITE_KEYS.items():
            result.setdefault(key, default)

    return result


# Documents that have a dedicated slot in the "documents" dict (Rule 6).
# A cert-like line mentioning one of these unambiguously belongs there,
# not in "certifications" -- confirmed as a real, visible quality
# problem on rendered output: LLMs regularly return passport/CDC/INDOS
# as {"type": ..., "number": ...} entries inside "certifications" (see
# _stringify_cert) despite Rule 4 explicitly saying not to, and a
# recruiter scanning "Certifications" for STCW/GMDSS-type items sees
# passport/visa numbers mixed in instead.
_DOCUMENT_SLOT_KEYWORDS = {
    'passport': 'passport',
    'indos':    'indos',
    'cdc':      'cdc',
}
# Document-like items with no dedicated schema slot -- filed under
# documents['other'] (a freeform list generate_docx also renders under
# "Documents & License") rather than left cluttering certifications.
_DOCUMENT_OTHER_KEYWORDS = ('visa', 'yellow fever', 'vaccination', "seaman's book", 'seafarer identity')


def _reclassify_document_like_certs(result: dict) -> dict:
    """
    Moves cert-like entries that are actually documents (passport, visa,
    CDC, INDOS, yellow fever) out of "certifications" and into
    "documents". Only reclassifies, never drops -- every item that
    leaves certifications lands somewhere in documents, so
    enforce_cert_completeness's coverage check (which looks at both)
    still sees it as present.
    """
    certs = result.get('certifications')
    if not isinstance(certs, list) or not certs:
        return result

    documents = result.get('documents')
    if not isinstance(documents, dict):
        documents = {}
    result['documents'] = documents

    kept = []
    for cert in certs:
        cert_str = str(cert)
        cert_lower = cert_str.lower()
        matched = False
        for keyword, slot in _DOCUMENT_SLOT_KEYWORDS.items():
            if keyword in cert_lower:
                value = re.sub(rf'^\s*{keyword}\S*\s*[:\-|]*\s*', '', cert_str, flags=re.IGNORECASE).strip(' |-')
                if not documents.get(slot):
                    documents[slot] = value or cert_str
                matched = True
                break
        if matched:
            continue
        for keyword in _DOCUMENT_OTHER_KEYWORDS:
            if keyword in cert_lower:
                documents.setdefault('other', [])
                if cert_str not in documents['other']:
                    documents['other'].append(cert_str)
                matched = True
                break
        if not matched:
            kept.append(cert)

    result['certifications'] = kept
    return result


def _cert_item_covered(item: str, coverage_text: str) -> bool:
    """
    Token-overlap check, not exact/substring match -- a rewritten cert
    line is allowed to be reformatted or reordered ('CDC | MUM 549153 |
    16/APR/2034' vs the source's 'CDC No. MUM549153 valid till 16 Apr
    2034'), so this only asks whether most of the item's meaningful
    (3+ char, non-noise) words survive somewhere in the final output --
    not that the exact string does. An item with no meaningful tokens at
    all (pure table noise) can't be checked either way, so it's treated
    as covered rather than flagged.

    A distinctive alphanumeric code (mixes letters and digits -- a
    passport/CDC/INDOS number, e.g. "x0000000") counts as covered on its
    own if it appears anywhere in the output, without also requiring the
    surrounding words ("Passport No. ... Exp ...") to match. Confirmed as
    a real false-positive without this: the rewrite's "documents" dict
    keeps a document under its NUMBER, not its source line's label/expiry
    ({"passport": "x0000000"} has no word "passport" or "exp" anywhere in
    it) -- requiring 60% of every token including the label and expiry
    date flagged an already-covered document as dropped. A document/cert
    number is specific enough that a coincidental match elsewhere is
    vanishingly unlikely, so it's trusted as strong evidence by itself.
    """
    item_norm = re.sub(r'[^a-z0-9 ]', ' ', item.lower())
    tokens = [t for t in item_norm.split() if len(t) >= 3 and t not in _CERT_TABLE_NOISE]
    if not tokens:
        return True
    code_tokens = [t for t in tokens if re.search(r'[a-z]', t) and re.search(r'\d', t)]
    if code_tokens and any(t in coverage_text for t in code_tokens):
        return True
    hits = sum(1 for t in tokens if t in coverage_text)
    return (hits / len(tokens)) >= 0.6


# Zero-width space/joiner/non-joiner and BOM -- invisible in any
# renderer but NOT stripped by str.strip() (none carry Unicode's
# White_Space property despite the "space" in some of their names).
# Confirmed leaking into real extracted section text as a stray prefix.
_INVISIBLE_CHARS = re.compile('[​‌‍﻿]')


def _looks_like_real_source_line(line: str) -> bool:
    """
    Rejects lines that are structural extraction noise rather than a
    real certificate/course/document entry, before enforce_cert_
    completeness ever treats one as something to check coverage for (and
    potentially append verbatim to the candidate's visible output).
    Confirmed on real extracted section text, three concrete patterns:

    1. A bare date with nothing else ("21/12/1999") -- a floating
       fragment (e.g. a header-row date of birth) that ended up in this
       section's text, never a real cert/document line on its own.
    2. A single "word" (no internal space) that's letters directly
       butted against a long digit run with no separator -- a structural
       artifact (two unrelated table fields concatenated with zero
       character-gap in the source PDF; see pipeline/extractor.py's
       _split_glued_words, which fixes the common case at the source,
       but a differently-shaped resume could still produce this).
    3. A short (<=2 word) fragment with no digit and no acronym-like
       (short, all-caps) token -- an isolated stray word swept in from
       an adjacent section by a column-bleed or line-grouping artifact
       (e.g. "hands-", "industry", "system"). Genuine short cert/document
       lines in this domain are consistently acronym- or digit-bearing
       (STCW, CDC, INDOS No. 24EM0438), so this rarely rejects a real one.

    Zero-width and other invisible Unicode characters are stripped
    before any of the above checks run -- confirmed as a real gap on the
    exact real fragment this was built against: Python's str.strip()
    does NOT remove U+200B (zero-width space) since it has no
    White_Space property despite the name, so the bare-date check's
    fullmatch() silently failed against '​21/12/1999' (a real
    leading-zero-width-space-prefixed header-row DOB) until this was
    stripped first.
    """
    line = _INVISIBLE_CHARS.sub('', line).strip()
    if not line:
        return False
    if re.fullmatch(r'\d{1,2}/\d{1,2}/\d{2,4}', line):
        return False
    if ' ' not in line and re.search(r'[A-Za-z]{2,}\d{5,}|\d{5,}[A-Za-z]{2,}', line):
        return False
    words = line.split()
    has_digit   = any(c.isdigit() for c in line)
    has_acronym = any(len(w) >= 2 and w.isupper() for w in words)
    if len(words) <= 2 and not has_digit and not has_acronym:
        return False
    return True


_DANGLING_LINE_CONNECTORS = {'&', 'and', 'or'}


def _merge_wrapped_cert_lines(lines: list) -> list:
    """
    A certificate name that wraps across two source lines must not
    become two separate source items. Confirmed real bug (Ankur
    Shrivastava): source has "Passenger Familiarization and Crowd &" /
    "Crisis Management Certification." as two consecutive lines (a
    plain PDF line-wrap, not column bleed -- pipeline/parser.py's own
    section-split already produces exactly these two lines). Coverage-
    checking and appending them as separate items let "Crisis
    Management" get silently treated as covered (it also appears as an
    unrelated skill, "Crisis Management" in the Skills list) while the
    orphaned first half, "Passenger Familiarization and Crowd &", was
    appended alone -- a visibly broken half-sentence in the candidate's
    output.

    Narrow signal, not "doesn't end in a period": a line ending in a
    bare coordinating connector ('&', 'and', 'or') is a strong sign it
    continues onto the next line. Many genuinely complete cert/document
    lines don't end in terminal punctuation either (e.g. "MEO CLASS II",
    a COC number ending in a date) -- merging on "no trailing period"
    would wrongly glue those unrelated lines together.
    """
    merged = []
    buffer = ''
    for line in lines:
        line = line.strip()
        if not line:
            continue
        buffer = f'{buffer} {line}'.strip() if buffer else line
        last_word = buffer.split()[-1].strip('.,;:').lower() if buffer.split() else ''
        if last_word in _DANGLING_LINE_CONNECTORS:
            continue
        merged.append(buffer)
        buffer = ''
    if buffer:
        merged.append(buffer)
    return merged


def enforce_cert_completeness(result: dict, parsed: dict) -> dict:
    """
    Deterministic safety net, not a prompt instruction -- an LLM cannot
    guarantee 100% retention by wording alone, and this is the actual
    reason qualified candidates get rejected downstream (a missing
    certificate number reads as disorganisation to a crewing manager,
    not as an AI-rewrite artifact they'd forgive).

    Extracts every certificate/course/document line from the ORIGINAL
    parsed source (before rewrite) -- the certifications, documents, and
    license sections all count, since real resumes scatter these across
    different headings (see parser.py's SECTION_HEADERS) and a rewrite
    that drops a passport number from a 'Documents' section is exactly
    as much a completeness failure as dropping a cert from
    'Certifications'. Checks each source line against the rewritten
    output's certifications, documents, and skills combined (a cert like
    'STCW' legitimately ends up in skills, not certifications, in some
    rewrites). Anything not found gets appended verbatim to
    certifications rather than silently shipped missing -- this never
    invents wording, it only restores a line the source already had.
    """
    sections = parsed.get('sections', {}) if isinstance(parsed, dict) else {}
    source_blob = '\n'.join([
        sections.get('certifications', ''),
        sections.get('documents', ''),
        sections.get('license', ''),
    ])

    # These are parser.py's best segmentation of the source into a
    # certifications/documents/license section, but never treat that as
    # a clean, line-per-item field on its own -- real bug fixed here:
    # confirmed on a real resume that a column-bleed or table-extraction
    # artifact upstream (a stray header-row DOB, an isolated word swept
    # in from Summary) can put genuinely non-cert text into this blob,
    # and this safety net was appending those lines verbatim into the
    # candidate's visible Certifications section. The extraction bugs
    # that caused THIS specific contamination are fixed at the source
    # (see pipeline/extractor.py's _column_bleed_penalty and
    # _split_glued_words), but this filter stays as defense-in-depth --
    # a line-based section split can still occasionally misattribute a
    # line on some other resume's layout, and this must never forward
    # that verbatim into user-facing output.
    source_items = []
    for line in _merge_wrapped_cert_lines(source_blob.split('\n')):
        line = line.strip()
        if len(line) < 4 or line.lower() in _CERT_TABLE_NOISE:
            continue
        if not _looks_like_real_source_line(line):
            continue
        source_items.append(line)

    certifications = result.get('certifications', [])
    if not isinstance(certifications, list):
        certifications = []
    certifications = _drop_fabricated_reference_numbers(certifications, parsed.get('raw_text', '') if isinstance(parsed, dict) else '')
    result['certifications'] = certifications

    if not source_items:
        return result

    documents = result.get('documents', {})
    if not isinstance(documents, dict):
        documents = {}
    skills = result.get('skills', [])
    if not isinstance(skills, list):
        skills = []

    coverage_text = ' '.join(str(x) for x in (
        list(certifications) + list(documents.values()) + list(skills)
    )).lower()
    coverage_text = re.sub(r'[^a-z0-9 ]', ' ', coverage_text)

    appended = [item for item in source_items if not _cert_item_covered(item, coverage_text)]
    if appended:
        certifications.extend(appended)
        result['certifications'] = certifications
        log.warning(json.dumps({
            'event':          'cert_completeness_safety_net_triggered',
            'appended_count': len(appended),
        }))
        # Real bug fixed, confirmed on real output: _reclassify_document_
        # like_certs already runs once, earlier, inside
        # _normalize_rewrite_result -- but that's BEFORE this function
        # ever appends anything, so a restored source line that's
        # actually a passport/visa/CDC/INDOS number (not a real
        # certificate) never gets the same document-vs-certificate
        # reclassification the model's OWN output already receives.
        # Confirmed on a real resume: Passport/US VISA/CDC numbers
        # restored by this safety net stayed in "certifications" and
        # cluttered the rendered Certifications section, exactly the
        # problem _reclassify_document_like_certs exists to prevent.
        # Re-running it now catches anything JUST appended.
        result = _reclassify_document_like_certs(result)

    return result


def enforce_education_completeness(result: dict, parsed: dict) -> dict:
    """
    Same deterministic restore-don't-invent safety net as
    enforce_cert_completeness/enforce_language_completeness, applied to
    "education" -- see _DEFAULTABLE_REWRITE_KEYS's comment for why
    "education" needed this (confirmed on real output: some models omit this
    field even when not truncated).
    """
    education_section = parsed.get('sections', {}).get('education', '') if isinstance(parsed, dict) else ''
    if not education_section or not education_section.strip():
        return result

    source_items = [
        line.strip() for line in education_section.split('\n')
        if len(line.strip()) >= 4 and _looks_like_real_source_line(line)
    ]
    if not source_items:
        return result

    education = result.get('education', [])
    if not isinstance(education, list):
        education = []

    coverage_text = re.sub(r'[^a-z0-9 ]', ' ', ' '.join(str(x) for x in education).lower())

    appended = [item for item in source_items if not _cert_item_covered(item, coverage_text)]
    if appended:
        education.extend(appended)
        result['education'] = education
        log.warning(json.dumps({
            'event':          'education_completeness_safety_net_triggered',
            'appended_count': len(appended),
        }))

    return result


def enforce_language_completeness(result: dict, parsed: dict) -> dict:
    """
    Same deterministic restore-don't-invent safety net as
    enforce_cert_completeness, applied to "languages" -- confirmed
    necessary as some LLM responses omit "languages" from the JSON entirely (not
    malformed, just never produced for candidates whose source resume
    DOES list languages), which _DEFAULTABLE_REWRITE_KEYS silently covers
    with an empty list -- correct for a genuinely languages-less resume,
    silently wrong for one where the rewrite just dropped real source
    content.

    Deliberately does NOT reuse _looks_like_real_source_line here: that
    filter rejects short (<=2 word), non-acronym, non-digit lines as
    noise, which is right for a certifications/documents blob (real
    noise there looks exactly like that) but wrong for languages -- a
    single real language name ("English", "Hindi") is exactly a bare,
    non-acronym, non-digit one-word line, and would be incorrectly
    dropped as noise by that filter.
    """
    languages_section = parsed.get('sections', {}).get('languages', '') if isinstance(parsed, dict) else ''
    if not languages_section or not languages_section.strip():
        return result

    source_items = [
        line.strip() for line in languages_section.split('\n')
        if len(line.strip()) >= 2
    ]
    if not source_items:
        return result

    languages = result.get('languages', [])
    if not isinstance(languages, list):
        languages = []

    coverage_text = re.sub(r'[^a-z0-9 ]', ' ', ' '.join(str(x) for x in languages).lower())

    appended = [item for item in source_items if not _cert_item_covered(item, coverage_text)]
    if appended:
        languages.extend(appended)
        result['languages'] = languages
        log.warning(json.dumps({
            'event':          'language_completeness_safety_net_triggered',
            'appended_count': len(appended),
        }))

    return result


RANK_TO_JD = {
    'master':                    'deck_officer',
    'chief officer':             'chief_officer',
    'second officer':            'second_officer',
    'third officer':             'deck_officer',
    'chief engineer':            'engineer',
    'second engineer':           'engineer',
    'third engineer':            'engineer',
    'fourth engineer':           'fourth_engineer',
    'eto':                       'eto',
    'electro technical officer': 'eto',
    'bosun':                     'able_seaman',
    'ab seaman':                 'able_seaman',
    'able seaman':               'able_seaman',
    'motorman':                  'rating',
    'fitter':                    'rating',
    'oiler':                     'oiler',
    'deck cadet':                'deck_cadet',
    'engine cadet':              'rating',
    'electrical cadet':          'eto',  # new tier, see rules/aliases.py -- closest existing JD template
    'jwko':                      'deck_officer',
}

MIN_SKILLS_BY_RANK = {
    'oiler': [
        'Engine Room Watch', 'Fuel Oil Transfer', 'Bilge Operations',
        'Lubrication Systems', 'Planned Maintenance System (PMS)',
        'MARPOL Compliance', 'ISM Code', 'Safety Drills', 'Pump Operations',
        'Valve Maintenance', 'Auxiliary Engine Maintenance', 'Watchkeeping',
        'Fire Fighting', 'Pressure Testing', 'Tool Handling',
    ],
    'eto': [
        'GMDSS Equipment Maintenance', 'High Voltage Systems', 'ECDIS',
        'AIS Maintenance', 'VDR Maintenance', 'Bridge Navigation Systems',
        'Automation Systems', 'PLC Troubleshooting', 'Electrical Fault Diagnosis',
        'ISM Code', 'Planned Maintenance System', 'UPS Systems',
        'Motor Rewinding', 'Alarm Monitoring', 'SOLAS Compliance',
    ],
    'fourth engineer': [
        'Auxiliary Engine Maintenance', 'Fuel Oil System', 'Purifier Operation',
        'Air Compressor Maintenance', 'Bilge System', 'Fresh Water Generator',
        'Watchkeeping', 'PMS', 'MARPOL Compliance', 'ISM Code',
        'Pump Maintenance', 'Engine Room Safety', 'Boiler Operation',
        'Hydraulic Systems', 'Oily Water Separator',
    ],
    'third officer': [
        'Navigation Systems', 'Chart Management', 'Cargo Handling Operations',
        'Stability Management', 'ISM Code', 'STCW Compliance',
        'Watchkeeping Procedures', 'Lifeboat Drills', 'Safety Protocols',
        'ECDIS Operation', 'Weather Routing', 'Port State Control',
        'Mooring Operations', 'Deck Maintenance', 'ISGOTT Standards',
    ],
    'third engineer': [
        'Engine Room Maintenance', 'Diesel Engine Systems', 'Auxiliary Equipment',
        'PMS Implementation', 'ISM Code', 'STCW Compliance',
        'Pump and Valve Systems', 'Cooling Systems', 'Lubrication Systems',
        'MARPOL Procedures', 'Emergency Procedures', 'Condition Monitoring',
        'Electrical Systems', 'Mechanical Troubleshooting', 'Documentation',
    ],
}

_CERT_TABLE_NOISE = {
    'pass', 'fail', 'valid', 'expired', 'n/a', 'yes', 'no',
    'date', 'status', 'issue', 'expiry', 'issued', 'endorsement',
}

# Recognized bare (unparenthesized) maritime cert/course acronyms —
# used by enforce_minimum_quality's R23 acronym-format fix to wrap the
# first bare occurrence in parens when a cert line already names the
# acronym in plain text (e.g. "...STCW Courses") rather than a spelled-
# out phrase KNOWN_ACRONYMS can match.
BARE_ACRONYMS = [
    'STCW', 'GMDSS', 'ECDIS', 'GOC', 'CoC', 'ENG1', 'ISPS', 'SOLAS',
    'MARPOL', 'ISM', 'DP', 'BOSIET', 'HUET', 'ARPA', 'AIS', 'GOC',
    'BST', 'PSC', 'OOW', 'EOOW',
]


_ABBREVIATION_IN_PROGRESS = re.compile(r'^[A-Z](\.[A-Z])*$')


def split_into_sentences(text: str) -> list:
    """
    Splits text into sentences on '.', '!', '?' — but treats a period
    immediately following a single capital letter (e.g. 'M.T', 'M.V' —
    standard maritime vessel-prefix abbreviations) as not a sentence
    boundary. A naive split('.') over-counts fragments on any summary
    mentioning a vessel like 'M.T Example', which could let a summary
    with fewer than 3 real sentences skip the minimum-quality fallback
    below simply because the abbreviation inflated the fragment count.

    Real bug fixed, confirmed on real generated output: the original
    single-letter check only guarded ONE period per abbreviation, so
    'M.V.' (two periods -- written with a period after EACH letter,
    unlike 'M.T' with only one) still tripped a false boundary on its
    SECOND period, once the accumulated fragment became the two-
    character "word" 'M.V' rather than the single-character 'M' the
    original check looked for -- confirmed as a real summary truncated
    mid-vessel-name. Matches the whole abbreviation-so-far (one or more
    single uppercase letters each followed by a period) instead of only
    its first letter, so every period in a multi-letter abbreviation is
    covered, not just the first.
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


def _normalize_rank(rank) -> str:
    """Ensure rank is always a clean string regardless of what's passed in."""
    if isinstance(rank, str):
        return rank
    if isinstance(rank, dict):
        for key in ('rank', 'rank_detected'):
            value = rank.get(key)
            if isinstance(value, str):
                return value
    return ''


def sanitise_section(text: str, max_chars: int = 800) -> str:
    """
    Cleans garbled column-extracted text before sending to Claude.
    Removes certificate table cell dumps (Pass/Fail/date rows),
    short orphan lines from two-column PDF splitting.

    Truncation (when the cleaned text exceeds max_chars) cuts at the last
    complete " | "-joined line that fits, not a blind character slice —
    a raw [:max_chars] cut can land mid-word or mid-entry, and for the
    experience section specifically this was confirmed to silently drop
    an entire role's vessel/GRT/date details before the LLM ever saw
    them (not a rewrite-quality issue — the source data itself never
    reached the prompt).
    """
    if not text or text.strip() in ('', 'NOT PROVIDED'):
        return 'NOT PROVIDED'
    lines = text.split('\n')
    cleaned = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.lower() in _CERT_TABLE_NOISE:
            continue
        if len(stripped) < 4:
            continue
        cleaned.append(stripped)

    result = ' | '.join(cleaned)
    if len(result) <= max_chars:
        return result

    # Keep whole " | "-joined lines up to the budget, from the start
    # (earliest-listed content — typically the most recent role, per
    # standard reverse-chronological resume convention) rather than
    # cutting the joined string at an arbitrary character.
    kept = []
    running_len = 0
    for line in cleaned:
        added_len = len(line) + (3 if kept else 0)  # +3 for ' | ' separator
        if running_len + added_len > max_chars:
            break
        kept.append(line)
        running_len += added_len
    return ' | '.join(kept) if kept else result[:max_chars]


def build_rewrite_prompt(parsed: dict, issues: list, job_description: str,
                         rank: str = '', license_rank: str = '', sea_months: int = 0,
                         target_cruise_line: str = '', strict: bool = False) -> str:

    rank = _normalize_rank(rank)

    if not job_description.strip():
        rank_key = RANK_TO_JD.get(rank.lower(), 'deck_officer')
        job_description = MARITIME_JDS.get(rank_key, MARITIME_JDS['deck_officer'])

    # Optional layer on TOP of the rank-based JD above, never a
    # replacement for it -- MARITIME_JDS/rank-based matching is
    # permanent by design (see CLAUDE.md: seafarers submit one resume to
    # many agencies, there is no per-candidate JD to collect). This only
    # activates when the caller actually knows which specific cruise
    # line a submission targets (e.g. an MYC recruiter tailoring one
    # application), parallel to how target_rank already overrides
    # rank-detection optionally. An unrecognized/empty name resolves to
    # '' and this block is simply omitted -- graceful fallback to
    # rank-only behavior, not an error.
    cruise_line_block = ''
    resolved_line = resolve_cruise_line(target_cruise_line)
    if resolved_line:
        line_data = CRUISE_LINE_KEYWORDS[resolved_line]
        cruise_line_block = f"""
TARGET CRUISE LINE: {line_data['display_name']}
This candidate's resume is being tailored for a specific application to
{line_data['display_name']}. Use the terminology below to phrase the
candidate's OWN real, already-grounded experience/certs/skills in the
vocabulary this line's ATS and recruiters actually scan for — this is a
WORD-CHOICE preference, not a new source of facts. Rule 0 (grounding)
and Rule 10 (never invent experience/certs) below still apply without
exception: never add a skill, cert, or experience claim the candidate's
real source data doesn't already support, even if it's on this list.
Universal cruise-industry certs/keywords (relevant regardless of line):
{', '.join(UNIVERSAL_CRUISE_KEYWORDS['keywords'])}
{line_data['display_name']}-specific keywords (use where the candidate's
real experience genuinely supports the phrasing):
{', '.join(line_data['keywords'])}
{line_data['display_name']}-specific certifications this line emphasizes
(only mention ones the candidate's real source data already shows):
{', '.join(line_data['certifications'])}
"""
        if line_data['tone']:
            cruise_line_block += f"Resume tone/emphasis this line favors: {', '.join(line_data['tone'])}\n"

    sections        = parsed.get('sections', {})
    contact         = parsed.get('contact', {})
    skills          = parsed.get('skills', [])
    raw_text        = parsed.get('raw_text', '')
    rank_lower      = rank.lower() if rank else ''
    min_skills      = MIN_SKILLS_BY_RANK.get(rank_lower, [])
    min_skills_text = ', '.join(min_skills) if min_skills else 'N/A'

    all_phones = contact.get('all_phones', [])
    phone_str  = ' / '.join(all_phones) if all_phones else contact.get('phone', 'NOT FOUND')

    license_line = (f'Highest License Held : {license_rank.title()}'
                    if license_rank and license_rank != rank else '')

    # Decide whether to use parsed sections or raw text fallback.
    # Raw fallback triggers when experience or summary content is thin —
    # this happens on heavily garbled two-column resumes (e.g. Harshal More ETO).
    # 1500 chars was confirmed too tight for a real multi-role service
    # record (4-5 roles with vessel names/GRT/dates easily exceeds it),
    # silently dropping older roles' vessel-specific details before the
    # LLM ever saw them. Claude's large context window (200k tokens)
    # has enormous headroom above this; raised to give real seafarer
    # service records room without materially affecting latency.
    experience_clean = sanitise_section(sections.get('experience', ''), 4000)
    summary_clean    = sanitise_section(sections.get('summary', ''))

    use_raw_fallback = (
        raw_text and (
            experience_clean == 'NOT PROVIDED'
            or len(experience_clean) < 150
            or summary_clean == 'NOT PROVIDED'
            or len(summary_clean) < 50
        )
    )

    if use_raw_fallback:
        source_block = (
            'RAW RESUME TEXT (extract and reconstruct all sections from this):\n'
            + sanitise_section(raw_text, 3000)
        )
    else:
        source_block = f"""ORIGINAL RESUME SECTIONS:
Summary              : {summary_clean}
Experience           : {experience_clean}
Additional Experience: {sanitise_section(sections.get('additional_experience', ''))}
Education            : {sanitise_section(sections.get('education', ''))}
Skills               : {sanitise_section(sections.get('skills', ''))}
Technical Expertise  : {sanitise_section(sections.get('technical_expertise', ''))}
Certifications       : {sanitise_section(sections.get('certifications', ''))}
Documents/License    : {sanitise_section(sections.get('documents', ''))} {sanitise_section(sections.get('license', ''))}
Languages            : {sanitise_section(sections.get('languages', ''))}"""

    prompt = f"""Rewrite this seafarer resume to be fully ATS-optimised. Return valid JSON only.

CANDIDATE DATA:
Name                 : {parsed.get('name', 'Unknown')}
Current Sailing Rank : {rank.title() if rank else 'Unknown'}
{license_line}
Email                : {contact.get('email', 'NOT FOUND IN RESUME')}
Phone                : {phone_str}
Total Sea Service (verified, use this exact figure): {sea_months} months
Skills Detected      : {', '.join(skills)} ({len(skills)} total)
Minimum Skills       : {min_skills_text}

{source_block}

ATS ISSUES TO FIX:
{chr(10).join(issues) if issues else 'No major issues detected'}

TARGET JOB DESCRIPTION:
{job_description}
{cruise_line_block}
{CRITICAL_TRUTHFULNESS_RULES}
{STRICT_RETRY_ADDENDUM if strict else ''}
REWRITING RULES — FOLLOW EXACTLY:
0. GROUNDING — applies to every sentence in the Summary and every bullet
   in Experience: each one must be traceable to a specific fact already
   present in the source data below (a vessel name, GRT, cert name,
   duty, date, number, or equipment brand). If a sentence would be
   equally true of any other candidate at this rank, it is too generic
   — rewrite it around an actual fact from the source, or cut it.
   Never use these filler phrases regardless of how common they are in
   real resumes, unless the source gives specific evidence for the exact
   claim (not just the general idea): "dedicated professional", "proven
   track record", "team player", "detail-oriented", "results-driven",
   "highly motivated", "excellent communication skills", "quick
   learner", "hard worker", "passionate about", "strong work ethic".
   The source resume text may itself contain phrases like these (real
   source resumes often do) — copying them into the rewrite doesn't
   satisfy this rule; replace them with something specific to this
   candidate's actual experience, or omit them.

1. Summary: EXACTLY 3 sentences, written naturally in confident, direct
   professional prose — not a fill-in-the-blank template restated the same
   way for every candidate. Cover, across the three sentences: (a) rank,
   total sea service duration, and vessel types sailed, (b) top
   certifications held, (c) one genuine strength evident from the actual
   experience (e.g. cargo operations, engine room management, bridge
   watchkeeping, safety compliance) — do not default to a generic phrase
   like "strong communication skills" unless the source actually supports
   it. Vary sentence structure and word choice candidate to candidate.
   For sea service duration specifically: use the exact "Total Sea
   Service (verified)" figure given above, converted to whatever unit
   reads naturally (e.g. 137 months as "over 11 years"). Do NOT add up
   the individual role date ranges yourself to estimate a duration —
   confirmed on real output that this produces a number wildly wrong
   from the verified figure (one real case stated "24 months" for a
   candidate whose verified total was 137 months), since overlapping
   contracts, gaps between contracts, and roles that continue past what
   fits in the summary all make a manual add-up unreliable. The verified
   figure already accounts for all of that.
   If candidate holds a higher license than their sailing rank, work this
   in naturally rather than as a bolted-on clause, e.g. 'Third Engineer
   holding Second Engineer (MEO Class II) CoC...'.
   Do NOT restate the candidate's name as the subject of more than one
   sentence (e.g. never "Kishore possesses... Kishore has demonstrated...
   As a skilled ETO, Kishore has..."). Standard resume convention either
   drops the subject entirely or opens with the rank/title, e.g. 'Third
   Officer with 5 years of sea service...' or 'Experienced Electro
   Technical Officer with...' — use the name at most once, if at all.
   Refer to vessel TYPES only in the summary (e.g. "Chemical/Oil Products
   Tankers and Bulk Carriers"), never specific vessel names — those
   belong in the Experience bullets (Rule 2), which already require them
   with exact GRT. A real generated summary once introduced a stray space
   into a vessel prefix mid-rewrite ("M. T SG Pegasus" instead of the
   source's "M.T SG Pegasus"); keeping vessel names out of the summary
   entirely removes that risk rather than relying on careful copying.

2. Experience bullets: Open each bullet with a strong, specific action
   verb, and vary the verb across bullets — do not open two bullets (in
   the same role or across different roles) with the same verb. Choose
   from verbs that fit the actual duty described, for example: Commanded,
   Directed, Navigated, Operated, Maintained, Oversaw, Coordinated,
   Executed, Managed, Supervised, Conducted, Inspected, Monitored,
   Diagnosed, Repaired, Enforced, Led, Trained, Streamlined — or an
   equally strong, specific alternative.

   Quantify wherever the source already provides a number — crew size
   managed, cargo volume/tonnage, GRT/DWT, number of vessels, inspection
   or safety record, contract duration. Never invent a number that isn't
   in the source; omit the metric entirely rather than estimate it.

   VESSEL DETAILS MANDATORY: every role entry must state the vessel
   name(s) and GRT somewhere in its bullets, phrased as part of a natural
   sentence about what was actually done — not a bolted-on fragment, and
   not the same sentence pattern repeated for every entry. Never
   generalise to just vessel types — use actual names from source.
   Maximum 3 bullets per role. Include ALL vessel entries from source —
   do not drop any.

   NO CROSS-ROLE REPETITION: do not reuse the same clause or sentence
   across bullets in DIFFERENT roles, even when the underlying duties are
   genuinely similar (e.g. a Cadet role and the Officer role that
   followed it at the same company). Reusing a phrase like "ensuring
   compliance with safety and environmental regulations" or "including
   high voltage equipment and automation systems" verbatim (or just
   reordered) across two role entries reads as templated, not written for
   this candidate. Instead, let each role's bullets reflect what's
   actually distinct about it — the specific equipment, the scope of
   responsibility, or the level of supervision — even where the general
   subject matter overlaps between consecutive roles.

2b. ACCURACY IS MANDATORY:
   Copy vessel names, GRT numbers, company names, dates, and job titles EXACTLY
   as they appear in the source. Do NOT paraphrase, shorten, or approximate.
   If unclear in the source, copy as-is rather than guessing.
   Wrong: 'Pacific Shipping' when source says 'Eastern Pacific Shipping'
   Wrong: GRT 29733 when source says GRT 199631
   Wrong: cert year 2020 when source shows specific expiry dates
   "company" must never be an empty string when the source names any
   employer for that role. If source lists more than one manning
   agency/company for a single role (e.g. "AG Meritimes and Norse Ship
   Management"), copy the full multi-company string exactly as one
   "company" value — do not pick just one and do not leave it blank.

3. Skills: 15-18 items. Prioritize the candidate's OWN detected skills
   and equipment/duties evident from their actual experience — the job
   description below is a generic rank-level template (not written for
   this specific candidate), so use its terminology only to phrase or
   supplement the candidate's real skills for ATS matching, not as the
   primary source of what skills to list. Include full form AND
   acronym: 'Standards of Training (STCW)'. If fewer than 15 emerge from
   the candidate's own source data, use the minimum skills list above to
   round out the count.

4. Certifications: '[Full Name (ACRONYM)] | [Issuing Authority] | [Year]'. Max 8.
   Use the ACTUAL issuing authority from source (e.g. DG Shipping, MMD Mumbai).
   If issuer not found in source, use the relevant maritime authority — NOT 'Issuing Authority'.
   Do NOT add certifications not present in source. Do NOT invent ENG1 or ISM certs.
   Do NOT list visas (US Visa, UAE Visa) as certifications — they are documents.
   The source Courses/Certifications text may contain long reference
   numbers (13+ digits) next to a course title and institute name — copy
   that number ONLY as part of THAT exact course's own entry, in the
   position an issuing/reference number would go. Never output a
   reference number paired with a generic maritime acronym (ECDIS,
   GMDSS, ARPA, ISPS, etc.) that is not the actual course title next to
   that number in source — inventing a plausible-sounding acronym label
   for a bare reference number you don't have a real title for is
   fabrication, exactly as much as inventing a certificate from nothing.
   If a course's own title genuinely cannot be determined from source,
   omit that entry rather than labeling it with an unrelated acronym.

5. Education: ALL degrees, diplomas, pre-sea training from source.
   Format: '[Qualification] — [Institution] — [Year]'
   Copy qualification name and institution EXACTLY as written.

6. Documents: CoC number, CDC number, INDOS, passport — copy EXACTLY from source.
   Do NOT invent or approximate document numbers.

7. Technical Expertise: Include brand-specific equipment list EXACTLY as in source.
   e.g. 'Main Engines - MAN B&W ME, Hyundai Himsen | Purifiers - Mitsubishi, Alfa Laval'
   If technical expertise section exists in source, it MUST appear in output.

8. Languages: Include all languages from source with proficiency level if given.

9. Past tense throughout. No personal pronouns (I, my, we).

10. Do NOT invent experience, vessels, certificates, companies or document numbers.

11. Mirror exact keywords from job description in summary, bullets, skills.

12. Length: max 2 pages. Junior ranks (cadet, oiler, motorman, ab, fitter):
    max 3 vessel entries, 15 skills, 3-sentence summary.

13. issues_fixed: only list things actually changed in the content.
    Do NOT claim to fix missing email/linkedin/phone if not in source.

CRITICAL OUTPUT SHAPE — every key below must be a FLAT TOP-LEVEL key of
the JSON object you return. Do NOT nest the payload inside a wrapper
object such as {{"candidate": {{...}}}} or {{"resume": {{...}}}} — "summary",
"experience_bullets", "skills", etc. must be siblings at the root, not
children of some other key. Use the EXACT key names shown below, spelled
and cased exactly as given — write "experience_bullets" not "experience",
"technical_expertise" not "technicalExpertise", never camelCase, and
never a synonym of your own choosing ("professional_summary" instead of
"summary", "core_competencies"/"technical_skills" instead of "skills",
"professional_experience" instead of "experience_bullets",
"certifications_and_training" instead of "certifications" — use ONLY the
exact names given). The JSON object must contain EXACTLY these top-level
keys and no others — do NOT invent additional top-level keys such as
"personal_information" (name/contact are handled outside this JSON
entirely — omit them here) or "sea_service_summary" (total sea service
is given to you above as a fact, not something to restate as its own
JSON field). Inside each experience_bullets entry, use "role"/"dates"/
"bullets" exactly — not "rank"/"job_title", not "startDate"+"endDate" or
"start_date"+"end_date", not "responsibilities"/"key_responsibilities".
"role" is the exact job title held during that entry — it is never an
empty string; if unsure, use the candidate's Current Sailing Rank given
above. Do NOT add a separate "vessels_served" (or similarly-named) field
for vessel names — every vessel name and its GRT belongs INSIDE that
entry's own "bullets" text, exactly as Rule 2 (VESSEL DETAILS MANDATORY)
already requires; a vessel name that exists only in some other field you
invented is the same as it not appearing in the output at all. Passport,
visa, CDC, and INDOS numbers belong ONLY under "documents" (Rule 6) —
never as entries in "certifications" (Rule 4).

FINAL CHECK BEFORE YOU WRITE "summary" — banned filler phrases (Rule 0),
repeated here because this is the field they leak into most often:
never "dedicated professional", "proven track record", "team player",
"detail-oriented", "results-driven", "highly motivated", "excellent
communication skills", "quick learner", "hard worker", "passionate
about", "strong work ethic", or any close paraphrase of these — every
clause must tie to a specific fact from the source data above.

FINAL CHECK BEFORE YOU WRITE EACH "bullets" ENTRY — this role's vessel
name and GRT (Rule 2) and a concrete number wherever the source has one
(crew size, cargo tonnage, GRT/DWT, contract length in months, equipment
count) MUST appear in that entry's own bullets, not just somewhere else
in the resume — a bullet describing a duty with no name/number attached
reads as a vague, un-quantified duty description even if a number
appears elsewhere in the document.

RETURN ONLY this JSON — start with {{ end with }} — no other text:
{{
    "summary": "3-sentence professional summary — no banned filler phrases, see FINAL CHECK above",
    "rank": "{rank}",
    "experience_bullets": [
        {{
            "role": "exact job title from source — never empty",
            "company": "exact company name from source",
            "dates": "MM/YYYY - MM/YYYY",
            "bullets": ["bullet naming this role's vessel + GRT (see FINAL CHECK above)", "bullet 2", "bullet 3"]
        }}
    ],
    "skills": ["skill1", "skill2"],
    "certifications": ["Full Name (ACRONYM) | Actual Issuing Authority | Year"],
    "education": ["Exact Qualification — Exact Institution — Year"],
    "documents": {{
        "coc": "exact COC number and expiry from source, else empty string",
        "cdc": "exact CDC number and expiry from source, else empty string",
        "indos": "exact INDOS number from source, else empty string",
        "passport": "exact passport number and expiry from source, else empty string"
    }},
    "languages": ["Language — Proficiency level"],
    "technical_expertise": ["Brand-specific equipment exactly as in source"],
    "issues_fixed": ["list of what was actually changed"]
}}"""
    return prompt


def enforce_minimum_quality(result: dict, rank: str, generic_mode: bool = False) -> dict:
    """Safety net: patches LLM output that didn't follow minimum quality rules."""
    if not isinstance(rank, str):
        rank = ''
    result_rank = result.get('rank', '')
    if not isinstance(result_rank, str):
        result['rank'] = rank
    rank_lower = rank.lower() if rank else ''

    # Fix rank title — "eto" → "Electro Technical Officer"
    RANK_TITLES = {
        'eto': 'Electro Technical Officer',
        'ab seaman': 'Able Seaman',
        'jwko': 'Junior Watchkeeping Officer',
    }
    if result.get('rank', '').lower() in RANK_TITLES:
        result['rank'] = RANK_TITLES[result['rank'].lower()]

    # Rule 3: Skills minimum 15
    skills = result.get('skills', [])
    # De-duplicate case-insensitively first — confirmed on real generated
    # output: raw (uncorrected) LLM output repeated "Chemical Tanker",
    # "Oil Tanker", and "Crisis Management" verbatim in the same Skills list.
    # The padding loop below only guards against adding a NEW duplicate; it
    # never removes ones the LLM already produced on its own.
    seen = set()
    deduped_skills = []
    for skill in skills:
        key = skill.lower().strip() if isinstance(skill, str) else skill
        if key not in seen:
            seen.add(key)
            deduped_skills.append(skill)
    skills = deduped_skills
    result['skills'] = skills
    if not generic_mode and len(skills) < 15:
        min_skills = MIN_SKILLS_BY_RANK.get(rank_lower, [])
        existing_lower = {s.lower() for s in skills}
        for skill in min_skills:
            if skill.lower() not in existing_lower and len(skills) < 15:
                skills.append(skill)
        result['skills'] = skills

    # Rule 1: Summary minimum 3 sentences
    summary = result.get('summary', '')
    sentences = split_into_sentences(summary)
    if not generic_mode and len(sentences) < 3:
        rank_title = RANK_TITLES.get(rank_lower, rank.title()) if rank else 'Seafarer'
        rank_key   = RANK_TO_JD.get(rank_lower, 'deck_officer')
        fallbacks = {
            'eto':          (f'Electro Technical Officer with sea service experience in marine electrical and navigation systems. '
                             f'Holds valid Certificate of Competency (CoC) and Standards of Training (STCW). '
                             f'Experienced in GMDSS, ECDIS, AIS, VDR maintenance, and high voltage systems.'),
            'engineer':     (f'{rank.title()} with sea service experience in engine room operations. '
                             f'Holds valid Certificate of Competency (CoC) and Standards of Training (STCW). '
                             f'Proficient in main propulsion, auxiliary machinery, and planned maintenance systems.'),
            'deck_officer': (f'{rank.title()} with sea service experience in bridge watchkeeping and navigation. '
                             f'Holds valid Certificate of Competency (CoC) and Standards of Training (STCW). '
                             f'Proficient in ECDIS, COLREGS, MARPOL, and ISM Code compliance.'),
        }
        result['summary'] = fallbacks.get(rank_key, fallbacks['deck_officer'])

    # Rule 4: Fix certifications — remove placeholders, fix acronyms
    certs = result.get('certifications', [])
    if certs:
        KNOWN_ACRONYMS = {
            'standards of training': 'STCW',
            'certificate of competency': 'CoC',
            'general operator certificate': 'GOC',
            'global maritime distress and safety system': 'GMDSS',
            'global maritime distress': 'GMDSS',
            'electronic chart display': 'ECDIS',
            'basic offshore safety': 'BOSIET',
            'officer of the watch': 'OOW',
            'engineer officer of the watch': 'EOOW',
        }
        fixed_certs = []
        for cert in certs:
            if not cert:
                continue
            # Replace placeholder issuer
            cert = cert.replace('Actual Issuing Authority', 'DG Shipping')
            cert = cert.replace('Issuing Authority', 'DG Shipping')
            # Replace placeholder year
            import re
            cert = re.sub(r'\|\s*Year\s*$', '| —', cert)
            # Fix GMDSS GOC naming
            if 'global maritime distress and safety system' in cert.lower() and 'goc' not in cert.lower() and 'operator' not in cert.lower():
                cert = cert.replace(
                    'Global Maritime Distress and Safety System (GMDSS)',
                    'GMDSS General Operator Certificate (GOC)'
                )
            # Add acronym if missing
            if '(' not in cert:
                cert_lower = cert.lower()
                for phrase, acronym in KNOWN_ACRONYMS.items():
                    if phrase in cert_lower:
                        if '|' in cert:
                            parts = cert.split('|', 1)
                            cert = f"{parts[0].strip()} ({acronym}) | {parts[1].strip()}"
                        else:
                            cert = f"{cert} ({acronym})"
                        break
            # R23 (scorer.py) checks for a parenthesized acronym anywhere
            # in the certifications block -- KNOWN_ACRONYMS above only
            # catches spelled-out phrases ("Standards of Training"), not
            # a line that already names the acronym in bare form, e.g.
            # "All required Basic & Advanced STCW Courses" (confirmed on
            # real generated output). Wrap the first bare acronym found
            # in parens rather than leave it unrecognized as "missing".
            if '(' not in cert:
                for acronym in BARE_ACRONYMS:
                    m = re.search(rf'\b{re.escape(acronym)}\b', cert, re.IGNORECASE)
                    if m:
                        cert = cert[:m.start()] + f'({cert[m.start():m.end()]})' + cert[m.end():]
                        break
            fixed_certs.append(cert)
        result['certifications'] = fixed_certs

    # Rule 2: Experience fallback — ONLY if experience_bullets is truly empty
    # Do NOT fire if the LLM returned entries with empty fields (Abhijit case)
    exp = result.get('experience_bullets', [])
    # Check if any entry has real company/dates (not fallback placeholders).
    # Real crash fixed: the has_real_exp check had no guard against an
    # experience_bullets entry that arrived as a plain string instead of
    # a dict. Guard against non-dict entries.
    has_real_exp = any(
        isinstance(e, dict)
        and e.get('company', '') not in ('', 'See service record', 'Manning Agency')
        and e.get('dates', '') not in ('', 'See service record')
        for e in exp
    )
    if not generic_mode and (not exp or not has_real_exp):
        # Only fire if truly no content — Abhijit has real source, so don't overwrite
        # with generic. Instead, leave empty and let scorer flag it for manual review.
        # Only use generic fallback if experience was genuinely empty in source
        if not exp:
            rank_key = RANK_TO_JD.get(rank_lower, 'deck_officer')
            generic_bullets = {
                'eto': ['Maintained GMDSS, ECDIS, AIS, and VDR systems onboard commercial vessel.',
                        'Ensured compliance with ISM Code and SOLAS electrical safety regulations.',
                        'Performed planned maintenance on high voltage switchgear and automation systems.'],
                'engineer': ['Operated and maintained main propulsion and auxiliary machinery.',
                             'Ensured MARPOL compliance and maintained planned maintenance system (PMS).',
                             'Conducted routine inspections and troubleshooting.'],
                'deck_officer': ['Performed navigational watchkeeping duties ensuring COLREGs compliance.',
                                 'Operated ECDIS and radar for safe vessel passage.',
                                 'Ensured ISM Code, MARPOL, and SOLAS compliance.'],
            }
            result['experience_bullets'] = [{
                'role': RANK_TITLES.get(rank_lower, rank.title()),
                'company': 'See original resume',
                'dates': 'See original resume',
                'bullets': generic_bullets.get(rank_key, generic_bullets['deck_officer']),
            }]
            exp = result['experience_bullets']

    # Real bug fixed: this used to live inside the `if not exp or not
    # has_real_exp:` branch above, as the `else` case -- which only runs
    # when has_real_exp is FALSE, i.e. when NO entry has real company/
    # dates. That's structurally unreachable for the actual failure this
    # was written for (confirmed on real generated output, Mrinal Thapa):
    # an entry with perfectly real company/dates but an empty 'role'
    # makes has_real_exp TRUE, so the whole outer `if` is skipped and this
    # backfill never ran at all -- the role stayed blank in both the Sea
    # Service Record row and Work Experience's bold header line. This is
    # now a separate, unconditional pass over every entry, independent of
    # whether any entry counts as "real" by the company/dates check.
    # Rather than invent a job title, falls back to the candidate's own
    # rank (already known, not a guess) -- reasonable since this is
    # almost always their most recent/current role.
    for e in exp:
        if isinstance(e, dict) and not str(e.get('role', '')).strip():
            e['role'] = RANK_TITLES.get(rank_lower, rank.title()) if rank else ('Professional' if generic_mode else 'Seafarer')

    return result


def _claude_text(response) -> str:
    parts = []
    for block in getattr(response, 'content', None) or []:
        text = getattr(block, 'text', None)
        if text:
            parts.append(text)
    return ''.join(parts)


def _as_openai_shaped_response(response):
    """Keep rewrite_resume's finish_reason / usage reads on a stable shape."""
    text = _claude_text(response)
    stop = getattr(response, 'stop_reason', None)
    finish = 'length' if stop == 'max_tokens' else stop
    usage = getattr(response, 'usage', None)
    prompt_tokens = getattr(usage, 'input_tokens', None) if usage else None
    completion_tokens = getattr(usage, 'output_tokens', None) if usage else None
    total = None
    if prompt_tokens is not None or completion_tokens is not None:
        total = (prompt_tokens or 0) + (completion_tokens or 0)
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=text),
            finish_reason=finish,
        )],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total,
        ),
    )


def build_generic_rewrite_prompt(parsed: dict, issues: list, job_description: str,
                                 strict: bool = False) -> str:
    from generic_ats.keywords import GENERIC_PROFESSIONAL_JD
    jd = sanitize_prompt_input((job_description or '').strip() or GENERIC_PROFESSIONAL_JD.strip())
    kw = extract_job_keywords(jd)
    raw = parsed.get('raw_text') or ''
    if not raw:
        sections = parsed.get('sections') or {}
        raw = '\n'.join(f'{k}: {v}' for k, v in sections.items() if v)
    return GENERIC_IMPROVE_PROMPT.format(
        truthfulness=CRITICAL_TRUTHFULNESS_RULES,
        strict_block=STRICT_RETRY_ADDENDUM if strict else '',
        job_description=jd,
        job_keywords=', '.join(kw.get('keywords') or []),
        original_resume=sanitise_section(raw, 4000),
    )


def _call_claude_with_retry(prompt: str, *, generic_mode: bool = False):
    """
    Calls Claude Messages API, retrying transient failures (timeouts,
    rate limits, connection errors) with exponential backoff (1s / 2s / 4s).

    Returns (openai-shaped response, retries_used).
    """
    if client is None:
        raise RuntimeError('Claude client unavailable')

    system = (
        'You ALWAYS respond with valid JSON only. '
        'No markdown. No explanation. No text before or after the JSON. '
        'Copy names, companies, dates, vessel names, GRT, and document numbers '
        'EXACTLY as given in the source — never approximate or invent them. '
        'Never invent qualifications, sea service, or certificates. '
        'Your response must start with { and end with }.'
    )
    if not generic_mode:
        system = 'You are a maritime resume writer. ' + system

    last_exc = None
    for retries_used, delay in enumerate((0,) + LLM_RETRY_BACKOFF_SECONDS):
        if delay:
            time.sleep(delay)
        try:
            raw_response = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=8192,
                temperature=0.3,
                system=system,
                messages=[{'role': 'user', 'content': prompt}],
            )
            return _as_openai_shaped_response(raw_response), retries_used
        except LLM_TRANSIENT_ERRORS as e:
            last_exc = e
        except APIStatusError as e:
            if not _is_retryable_status(e):
                raise
            last_exc = e
    raise last_exc


def rewrite_resume(parsed: dict, issues: list, job_description: str = '',
                   rank: str = '', license_rank: str = '', sea_months: int = 0,
                   target_cruise_line: str = '', strict: bool = False,
                   generic_mode: bool = False) -> dict:

    rank = _normalize_rank(rank)
    if generic_mode:
        prompt = build_generic_rewrite_prompt(parsed, issues, job_description, strict=strict)
    else:
        prompt = build_rewrite_prompt(
            parsed, issues, job_description, rank, license_rank, sea_months,
            target_cruise_line, strict=strict,
        )

    if len(prompt) > 20000:
        sections = parsed.get('sections', {})
        sections = {**sections, 'experience': sanitise_section(sections.get('experience', ''), 2500)}
        parsed = {**parsed, 'sections': sections}
        if generic_mode:
            prompt = build_generic_rewrite_prompt(parsed, issues, job_description, strict=strict)
        else:
            prompt = build_rewrite_prompt(
                parsed, issues, job_description, rank, license_rank, sea_months,
                target_cruise_line, strict=strict,
            )

    raw_text      = ''
    total_retries = 0
    for attempt in range(3):
        try:
            response, retries_used = _call_claude_with_retry(prompt, generic_mode=generic_mode)
            total_retries += retries_used

            raw_text = response.choices[0].message.content.strip()
            if not raw_text:
                continue

            if raw_text.startswith('```'):
                parts    = raw_text.split('```')
                raw_text = parts[1] if len(parts) > 1 else raw_text
                if raw_text.startswith('json'):
                    raw_text = raw_text[4:]
            raw_text = raw_text.strip()

            start = raw_text.find('{')
            end   = raw_text.rfind('}')
            if start != -1 and end != -1 and end > start:
                raw_text = raw_text[start:end + 1]

            result = json.loads(raw_text)

            finish_reason = None
            try:
                finish_reason = response.choices[0].finish_reason
            except Exception:
                pass

            result = _normalize_rewrite_result(result, finish_reason)

            missing_keys = REQUIRED_REWRITE_KEYS - result.keys()
            if missing_keys:
                usage_info = {}
                try:
                    usage = response.usage
                    usage_info = {
                        'prompt_tokens':     getattr(usage, 'prompt_tokens', None),
                        'completion_tokens': getattr(usage, 'completion_tokens', None),
                        'total_tokens':      getattr(usage, 'total_tokens', None),
                    }
                except Exception:
                    pass
                log.warning(json.dumps({
                    'event':             'incomplete_response',
                    'attempt':           attempt,
                    'finish_reason':     finish_reason,
                    **usage_info,
                    'missing_keys':      sorted(missing_keys),
                    'raw_response_len':  len(raw_text),
                }))
                if attempt == 2:
                    return {
                        'error':        'INCOMPLETE_RESPONSE',
                        'detail':       f'Response parsed as valid JSON but was missing required '
                                        f'fields (likely truncated mid-generation): {sorted(missing_keys)}',
                        'raw_response': raw_text[:500],
                        '_llm_retry_count': total_retries,
                    }
                continue

            result = enforce_minimum_quality(result, rank, generic_mode=generic_mode)
            result = enforce_cert_completeness(result, parsed)
            result = enforce_education_completeness(result, parsed)
            result = enforce_language_completeness(result, parsed)
            result = post_process_rewrite_result(result)
            if not result.get('rank'):
                result['rank'] = rank
            result['_llm_retry_count'] = total_retries
            return result

        except json.JSONDecodeError:
            if attempt == 2:
                return {
                    'error':        'JSON_PARSE_FAILED',
                    'raw_response': raw_text[:500],
                    'detail':       f'Failed after 3 attempts. Last: {raw_text[:200]}',
                    '_llm_retry_count': total_retries,
                }
            continue

        except Exception as e:
            return {'error': 'API_CALL_FAILED', 'detail': str(e), '_llm_retry_count': total_retries}

    return {
        'error':  'JSON_PARSE_FAILED',
        'detail': 'Empty response after 3 retries',
        '_llm_retry_count': total_retries,
    }