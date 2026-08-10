# services/ai_client.py
import json
import httpx
import re
import time
import logging
from groq import Groq, APIConnectionError, APITimeoutError, RateLimitError
from config import GROQ_API_KEY, GROQ_MODEL

log = logging.getLogger(__name__)

if not GROQ_API_KEY:
    # Same "warn, don't crash at import" pattern api.py uses for
    # MYC_API_KEY — every rewrite_resume() call will fail at the Groq
    # call (caught and handled via the existing AI-rewrite-unavailable
    # fallback), but this makes a missing key visible at startup in the
    # logs rather than only showing up as job-level Groq failures.
    log.warning('"GROQ_API_KEY not set — AI rewrite will fail for every job"')

def _build_groq_client():
    """
    Create a Groq client without crashing when ambient proxy config or API
    setup is problematic.

    Always constructs with an explicit httpx.Client(trust_env=False), so
    HTTP_PROXY/HTTPS_PROXY/ALL_PROXY (of any scheme) never affect this
    client. This is a single-VPS deployment with no documented need to
    route Groq's API calls through a proxy.
    """
    if not GROQ_API_KEY:
        return None

    try:
        return Groq(api_key=GROQ_API_KEY, http_client=httpx.Client(trust_env=False))
    except Exception as e:
        log.error('Failed to construct Groq client: %s', e, exc_info=True)
        return None

    try:
        return Groq(api_key=GROQ_API_KEY)
    except Exception as e:
        log.error('Failed to construct Groq client: %s', e, exc_info=True)
        return None


client = _build_groq_client()

# Transient failures worth retrying — timeouts, connection drops, rate limits.
# Non-transient errors (auth, bad request, etc.) propagate immediately since
# retrying them would just waste time before failing the same way.
GROQ_TRANSIENT_ERRORS = (APIConnectionError, APITimeoutError, RateLimitError)
GROQ_RETRY_BACKOFF_SECONDS = (1, 2, 4)

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


def split_into_sentences(text: str) -> list:
    """
    Splits text into sentences on '.', '!', '?' — but treats a period
    immediately following a single capital letter (e.g. 'M.T', 'M.V' —
    standard maritime vessel-prefix abbreviations) as not a sentence
    boundary. A naive split('.') over-counts fragments on any summary
    mentioning a vessel like 'M.T Example', which could let a summary
    with fewer than 3 real sentences skip the minimum-quality fallback
    below simply because the abbreviation inflated the fragment count.
    """
    if not text:
        return []
    text = text.replace('!', '.').replace('?', '.')
    tokens = text.split('.')
    sentences = []
    current = ''
    for i, tok in enumerate(tokens):
        current += tok
        if i == len(tokens) - 1:
            break
        last_word = current.split()[-1] if current.split() else ''
        if len(last_word) == 1 and last_word.isupper():
            current += '.'   # part of an abbreviation like "M." — don't split
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
    Cleans garbled column-extracted text before sending to Groq.
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
                         rank: str = '', license_rank: str = '') -> str:

    rank = _normalize_rank(rank)

    if not job_description.strip():
        rank_key = RANK_TO_JD.get(rank.lower(), 'deck_officer')
        job_description = MARITIME_JDS.get(rank_key, MARITIME_JDS['deck_officer'])

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
    # LLM ever saw them. llama-3.3-70b-versatile's real context window
    # (128k tokens on Groq) has enormous headroom above this; raised to
    # give real seafarer service records room without materially
    # affecting prompt size for the common case.
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
Skills Detected      : {', '.join(skills)} ({len(skills)} total)
Minimum Skills       : {min_skills_text}

{source_block}

ATS ISSUES TO FIX:
{chr(10).join(issues) if issues else 'No major issues detected'}

TARGET JOB DESCRIPTION:
{job_description}

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

RETURN ONLY this JSON — start with {{ end with }} — no other text:
{{
    "summary": "3-sentence professional summary",
    "rank": "{rank}",
    "experience_bullets": [
        {{
            "role": "exact job title from source",
            "company": "exact company name from source",
            "dates": "MM/YYYY - MM/YYYY",
            "bullets": ["bullet with vessel name + GRT", "bullet 2", "bullet 3"]
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


def enforce_minimum_quality(result: dict, rank: str) -> dict:
    """Safety net: patches Groq output that didn't follow minimum quality rules."""
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
    if len(skills) < 15:
        min_skills = MIN_SKILLS_BY_RANK.get(rank_lower, [])
        existing_lower = {s.lower() for s in skills}
        for skill in min_skills:
            if skill.lower() not in existing_lower and len(skills) < 15:
                skills.append(skill)
        result['skills'] = skills

    # Rule 1: Summary minimum 3 sentences
    summary = result.get('summary', '')
    sentences = split_into_sentences(summary)
    if len(sentences) < 3:
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
            fixed_certs.append(cert)
        result['certifications'] = fixed_certs

    # Rule 2: Experience fallback — ONLY if experience_bullets is truly empty
    # Do NOT fire if Groq returned entries with empty fields (Abhijit case)
    exp = result.get('experience_bullets', [])
    # Check if any entry has real company/dates (not fallback placeholders)
    has_real_exp = any(
        e.get('company', '') not in ('', 'See service record', 'Manning Agency')
        and e.get('dates', '') not in ('', 'See service record')
        for e in exp
    )
    if not exp or not has_real_exp:
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

    return result


def _call_groq_with_retry(prompt: str):
    """
    Calls the Groq chat completion endpoint, retrying transient failures
    (timeouts, rate limits, connection errors) with exponential backoff
    (1s / 2s / 4s). Exhausts all retries before propagating the last error.

    Returns (response, retries_used) — retries_used is 0 on a first-try
    success, so callers can track Groq retry counts per job.
    """
    if client is None:
        raise RuntimeError('GROQ client unavailable')

    last_exc = None
    for retries_used, delay in enumerate((0,) + GROQ_RETRY_BACKOFF_SECONDS):
        if delay:
            time.sleep(delay)
        try:
            response = client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[
                    {
                        'role': 'system',
                        'content': (
                            'You are a maritime resume writer. '
                            'You ALWAYS respond with valid JSON only. '
                            'No markdown. No explanation. No text before or after the JSON. '
                            'Copy vessel names, GRT, companies, dates, and document numbers '
                            'EXACTLY as given in the source — never approximate or invent them. '
                            'Your response must start with { and end with }.'
                        ),
                    },
                    {'role': 'user', 'content': prompt},
                ],
                temperature=0.3,
                # 3000 was tight for a full JSON payload covering up to 6
                # experience entries (3 bullets each), 18 skills, 8
                # certifications, education, documents, and languages —
                # a response cut off mid-generation can still parse as
                # valid JSON if the truncation point happens to land on
                # an earlier closing brace, silently dropping every field
                # after it (see the required-keys check below, which
                # catches this instead of relying on max_tokens alone).
                max_tokens=6000,
            )
            return response, retries_used
        except GROQ_TRANSIENT_ERRORS as e:
            last_exc = e
    raise last_exc


def rewrite_resume(parsed: dict, issues: list, job_description: str = '',
                   rank: str = '', license_rank: str = '') -> dict:

    rank   = _normalize_rank(rank)
    prompt = build_rewrite_prompt(parsed, issues, job_description, rank, license_rank)

    # Trim experience section if prompt is too long for Groq context.
    # 6000 chars (~1500 tokens) was confirmed drastically over-cautious
    # for llama-3.3-70b-versatile's real 128k-token context on Groq —
    # raised so this almost never triggers for a real resume. When it
    # still does (a genuinely extreme source), truncate with
    # sanitise_section's boundary-safe cut (whole lines, not mid-word)
    # instead of a raw [:1000] character slice, which could cut off an
    # entire role's vessel/GRT/date details mid-entry.
    if len(prompt) > 20000:
        sections = parsed.get('sections', {})
        sections = {**sections, 'experience': sanitise_section(sections.get('experience', ''), 2500)}
        parsed = {**parsed, 'sections': sections}
        prompt = build_rewrite_prompt(parsed, issues, job_description, rank, license_rank)

    raw_text      = ''
    total_retries = 0
    for attempt in range(3):
        try:
            response, retries_used = _call_groq_with_retry(prompt)
            total_retries += retries_used

            raw_text = response.choices[0].message.content.strip()
            if not raw_text:
                continue

            # Strip markdown fences if model adds them despite instructions
            if raw_text.startswith('```'):
                parts    = raw_text.split('```')
                raw_text = parts[1] if len(parts) > 1 else raw_text
                if raw_text.startswith('json'):
                    raw_text = raw_text[4:]
            raw_text = raw_text.strip()

            # Extract JSON boundaries — handles any preamble text
            start = raw_text.find('{')
            end   = raw_text.rfind('}')
            if start != -1 and end != -1 and end > start:
                raw_text = raw_text[start:end + 1]

            result = json.loads(raw_text)

            missing_keys = REQUIRED_REWRITE_KEYS - result.keys()
            if missing_keys:
                if attempt == 2:
                    return {
                        'error':        'INCOMPLETE_RESPONSE',
                        'detail':       f'Response parsed as valid JSON but was missing required '
                                        f'fields (likely truncated mid-generation): {sorted(missing_keys)}',
                        'raw_response': raw_text[:500],
                        '_groq_retry_count': total_retries,
                    }
                continue

            result = enforce_minimum_quality(result, rank)
            if not result.get('rank'):
                result['rank'] = rank
            result['_groq_retry_count'] = total_retries
            return result

        except json.JSONDecodeError:
            if attempt == 2:
                return {
                    'error':        'JSON_PARSE_FAILED',
                    'raw_response': raw_text[:500],
                    'detail':       f'Failed after 3 attempts. Last: {raw_text[:200]}',
                    '_groq_retry_count': total_retries,
                }
            continue

        except Exception as e:
            return {'error': 'API_CALL_FAILED', 'detail': str(e), '_groq_retry_count': total_retries}

    return {
        'error':  'JSON_PARSE_FAILED',
        'detail': 'Empty response after 3 retries',
        '_groq_retry_count': total_retries,
    }