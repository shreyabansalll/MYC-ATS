"""Regeneration / tailoring prompt DNA from Resume Matcher prompts/templates.py.

Used by services/ai_client.py for the generic (any-industry) rewrite path
and injected into the maritime rewrite as the never-invent rule block.
"""

CRITICAL_TRUTHFULNESS_RULES = """CRITICAL TRUTHFULNESS RULES - NEVER VIOLATE:
1. DO NOT add any skill, tool, technology, certificate, sea service, vessel,
   rank, or qualification that is not explicitly mentioned in the original resume
2. DO NOT invent numeric achievements (e.g. "increased by 30%", extra sea months)
   unless they exist in the original
3. DO NOT add company names, product names, vessel names, or technical terms
   not in the original
4. DO NOT upgrade experience level or rank (e.g. "Third Officer" -> "Chief Officer")
5. DO NOT add languages, frameworks, platforms, or certificates the candidate
   has not listed
6. DO NOT extend employment dates or change timelines. Copy date ranges exactly
   as they appear, including months.
7. You may rephrase existing bullet points to include keywords already supported
   by the source, but DO NOT invent entirely new responsibilities
8. Preserve factual accuracy - only use information provided by the candidate
9. NEVER remove existing skills, certifications, languages, or awards. You may
   reorder by relevance, but every original item must remain.

Violation of these rules could cause serious problems for the candidate in
job interviews or (for seafarers) regulatory audit.
"""

STRICT_RETRY_ADDENDUM = """
STRICT RETRY — the previous rewrite did not improve ATS match enough.
Do a heavier rewrite of wording ONLY:
- Rewrite every bullet as Challenge, Action, Result using facts already in the source.
- Put the target job title in the headline/summary if that title is already the
  candidate's real rank or listed role — never invent a title they do not hold.
- Weave in missing keywords from the job description ONLY where the source
  already supports that skill or duty.
- Keep the same number of experience entries. Do not drop vessels, employers,
  certificates, or dates.
- Still NEVER invent qualifications, sea service, or certificates.
"""


GENERIC_IMPROVE_PROMPT = """Tailor this resume for the job. Output ONLY a JSON object, no other text.

{truthfulness}

Rules:
- Make targeted adjustments to bullet points to align with job description phrasing.
  Preserve the candidate's original details — adjust wording, do not invent.
- DO NOT invent new information
- Preserve existing action verbs. Do not invent quantifiable achievements not in the original.
- Keep proper nouns (names, company names, locations, vessel names) unchanged
- Copy the date values EXACTLY as they appear in the original
- Do NOT use an em dash anywhere in the output
{strict_block}

Job Description:
{job_description}

Keywords to emphasize (only if already supported by resume content):
{job_keywords}

{protected_signals}

Original Resume:
{original_resume}

Return exactly this JSON shape:
{{
  "summary": "3 sentences, grounded in the source",
  "experience_bullets": [
    {{
      "role": "exact title from source",
      "company": "exact company from source",
      "dates": "exact dates from source",
      "bullets": ["action-verb bullet", "action-verb bullet"]
    }}
  ],
  "skills": ["skill from source"],
  "certifications": ["only certs present in source"],
  "education": ["education lines from source"],
  "documents": {{}},
  "languages": ["languages from source"],
  "issues_fixed": ["what you changed"]
}}
"""
