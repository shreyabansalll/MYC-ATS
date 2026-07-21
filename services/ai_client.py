# services/ai_client.py
import json
import re
import time
from groq import Groq, APIConnectionError, APITimeoutError, RateLimitError
from config import GROQ_API_KEY, GROQ_MODEL

client = Groq(api_key=GROQ_API_KEY)

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
    return result[:max_chars] if len(result) > max_chars else result


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
    experience_clean = sanitise_section(sections.get('experience', ''), 1500)
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
1. Summary: EXACTLY 3 sentences.
   '[Sailing Rank] with [X] years/months sea service on [vessel types]. Holds [top 3 certs]. [Key competency].'
   If candidate holds higher license than sailing rank:
   'Third Engineer holding Second Engineer (MEO Class II) CoC...'

2. Experience bullets: Active verb required. VESSEL DETAILS MANDATORY:
   First bullet MUST include vessel name and GRT:
   'Operated [systems] onboard [Vessel Name] (GRT [number]), a [vessel type].'
   Never generalise to just vessel types — use actual names from source.
   Maximum 3 bullets per role. Include ALL vessel entries from source — do not drop any.

2b. ACCURACY IS MANDATORY:
   Copy vessel names, GRT numbers, company names, dates, and job titles EXACTLY
   as they appear in the source. Do NOT paraphrase, shorten, or approximate.
   If unclear in the source, copy as-is rather than guessing.
   Wrong: 'Pacific Shipping' when source says 'Eastern Pacific Shipping'
   Wrong: GRT 29733 when source says GRT 199631
   Wrong: cert year 2020 when source shows specific expiry dates

3. Skills: 15-18 items using exact terminology from job description.
   Include full form AND acronym: 'Standards of Training (STCW)'.
   If fewer than 15 in source, use minimum skills list above.

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
    if len(skills) < 15:
        min_skills = MIN_SKILLS_BY_RANK.get(rank_lower, [])
        existing_lower = {s.lower() for s in skills}
        for skill in min_skills:
            if skill.lower() not in existing_lower and len(skills) < 15:
                skills.append(skill)
        result['skills'] = skills

    # Rule 1: Summary minimum 3 sentences
    summary = result.get('summary', '')
    sentences = [s.strip() for s in summary.replace('!', '.').replace('?', '.').split('.') if s.strip()]
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
                max_tokens=3000,
            )
            return response, retries_used
        except GROQ_TRANSIENT_ERRORS as e:
            last_exc = e
    raise last_exc


def rewrite_resume(parsed: dict, issues: list, job_description: str = '',
                   rank: str = '', license_rank: str = '') -> dict:

    rank   = _normalize_rank(rank)
    prompt = build_rewrite_prompt(parsed, issues, job_description, rank, license_rank)

    # Trim experience section if prompt is too long for Groq context
    if len(prompt) > 6000:
        sections = parsed.get('sections', {})
        sections['experience'] = sections.get('experience', '')[:1000]
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