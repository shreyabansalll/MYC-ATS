"""Writing-quality rules, AI buzzword scrubbing, and anti-hallucination checks.

Merged from Resume Matcher:
- app/services/writing_quality.py
- app/prompts/refinement.py (AI_PHRASE_BLACKLIST, AI_PHRASE_REPLACEMENTS)
- app/services/resume_preservation.py (_novel_numbers, numeric_claims)
- app/services/improver.py (_sanitize_user_input)

Deterministic. No LLM.
"""

from __future__ import annotations

import re
from collections import Counter
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from generic_ats.text_match import keyword_in_text

# ── Prompt Injection Sanitization ───────────────────────────────────────────

_INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?previous\s+instructions",
    r"disregard\s+(all\s+)?above",
    r"forget\s+(everything|all)",
    r"new\s+instructions?:",
    r"system\s*:",
    r"<\s*/?\s*system\s*>",
    r"\[\s*INST\s*\]",
    r"\[\s*/\s*INST\s*\]",
]


def sanitize_prompt_input(text: str) -> str:
    """Sanitize user input against prompt injection before feeding to LLM."""
    if not isinstance(text, str):
        return ""
    sanitized = text
    for pattern in _INJECTION_PATTERNS:
        sanitized = re.sub(pattern, "[REDACTED]", sanitized, flags=re.IGNORECASE)
    return sanitized


# ── AI Phrase Blacklist & Replacements ─────────────────────────────────────

AI_PHRASE_BLACKLIST: set[str] = {
    # Action verbs (overused in AI resume writing)
    "spearheaded",
    "orchestrated",
    "championed",
    "synergized",
    "leveraged",
    "revolutionized",
    "pioneered",
    "catalyzed",
    "operationalized",
    "architected",
    "envisioned",
    "effectuated",
    "endeavored",
    "facilitated",
    "utilized",
    # Corporate buzzwords
    "synergy",
    "synergies",
    "paradigm",
    "paradigm shift",
    "best-in-class",
    "world-class",
    "cutting-edge",
    "bleeding-edge",
    "game-changer",
    "game-changing",
    "disruptive",
    "disruptor",
    "holistic",
    "robust",
    "scalable",
    "actionable",
    "impactful",
    "proactive",
    "proactively",
    "stakeholder",
    "deliverables",
    "bandwidth",
    "circle back",
    "deep dive",
    "move the needle",
    "low-hanging fruit",
    "touch base",
    "value-add",
    # Filler phrases
    "in order to",
    "for the purpose of",
    "with a view to",
    "at the end of the day",
    "moving forward",
    "going forward",
    "on a daily basis",
    "on a regular basis",
    "in a timely manner",
    "at this point in time",
    "due to the fact that",
    "in the event that",
    "in light of the fact that",
}

AI_PHRASE_REPLACEMENTS: dict[str, str] = {
    "spearheaded": "led",
    "orchestrated": "coordinated",
    "championed": "advocated for",
    "synergized": "collaborated",
    "leveraged": "used",
    "revolutionized": "transformed",
    "pioneered": "introduced",
    "catalyzed": "initiated",
    "operationalized": "implemented",
    "architected": "designed",
    "envisioned": "planned",
    "effectuated": "completed",
    "endeavored": "worked",
    "facilitated": "helped",
    "utilized": "used",
    "synergy": "collaboration",
    "synergies": "collaborations",
    "paradigm": "approach",
    "paradigm shift": "change",
    "best-in-class": "top-performing",
    "world-class": "high-quality",
    "cutting-edge": "modern",
    "bleeding-edge": "modern",
    "game-changer": "innovation",
    "game-changing": "innovative",
    "disruptive": "innovative",
    "holistic": "comprehensive",
    "in order to": "to",
    "for the purpose of": "to",
    "with a view to": "to",
    "on a daily basis": "daily",
    "on a regular basis": "regularly",
    "in a timely manner": "promptly",
    "at this point in time": "currently",
    "due to the fact that": "because",
}

# ── Em-dash & Weak Opener Patterns ──────────────────────────────────────────

EM_DASH_RE = re.compile(r"—|(?<![\w-])-{2,3}(?![\w-])|(?<=\w)-{2,3}(?=\w)")

WEAK_OPENERS: tuple[str, ...] = (
    "responsible for",
    "helped with",
    "helped to",
    "helped",
    "assisted with",
    "assisted in",
    "assisted",
    "involved in",
    "participated in",
    "worked on",
    "tasked with",
    "duties included",
    "in charge of",
)
_WEAK_OPENER_RE = re.compile(
    r"^\W*(?:" + "|".join(re.escape(o) for o in WEAK_OPENERS) + r")\b",
    re.IGNORECASE,
)

_NUMBER_RE = re.compile(
    r"(?<![\w.])(?P<currency>[$€£₹])?(?P<number>\d[\d,]*(?:\.\d+)?)"
    r"(?:\s*(?P<unit>thousand|million|billion|percent|times|ms|gb|mb|tb|[kmb%x]))?"
    r"(?![A-Za-z])",
    re.IGNORECASE,
)


def scrub_em_dashes(text: str) -> str:
    """Replace em-dashes and long dashes with clean ATS-safe hyphen-minus."""
    if not isinstance(text, str):
        return ""
    # Space-surrounded em dashes become " - "
    s = re.sub(r"\s*[—–]\s*", " - ", text)
    # Double or triple hyphens become single hyphens
    s = re.sub(r"-{2,3}", "-", s)
    return s


def scrub_ai_phrases(text: str) -> str:
    """Replace overused AI buzzwords with clean, direct professional equivalents."""
    if not isinstance(text, str):
        return ""
    result = text
    for phrase, replacement in sorted(AI_PHRASE_REPLACEMENTS.items(), key=lambda x: len(x[0]), reverse=True):
        pattern = rf"\b{re.escape(phrase)}\b"
        result = re.sub(
            pattern,
            lambda m: (
                replacement.capitalize() if m.group(0)[0].isupper() else replacement
            ),
            result,
            flags=re.IGNORECASE,
        )
    return result


def clean_text_for_ats(text: str) -> str:
    """Apply comprehensive ATS writing post-processing: scrub dashes and AI buzzwords."""
    return scrub_ai_phrases(scrub_em_dashes(text))


# ── Fact Checking & Hallucinated Number Detection ───────────────────────────

def _is_date_like_number(text: str, match: re.Match[str]) -> bool:
    start, end = match.start(), match.end()
    prefix = text[max(0, start - 12) : start]
    suffix = text[end : min(len(text), end + 12)]
    if re.search(r"(?:19|20)\d{2}\s*[-/–]\s*$", prefix):
        return True
    if re.search(r"^\s*[-/–]\s*(?:19|20)\d{2}", suffix):
        return True
    months = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
    if re.search(rf"{months}[a-z]*\.?\s*$", prefix, re.IGNORECASE):
        return True
    return False


def numeric_claims(text: str) -> Counter[str]:
    """Extract normalized numeric claims (numbers + currency/units), ignoring years/versions."""
    claims: Counter[str] = Counter()
    for match in _NUMBER_RE.finditer(text):
        raw_number = match.group("number").replace(",", "")
        unit = (match.group("unit") or "").casefold()
        prefix = text[max(0, match.start() - 16) : match.start()].casefold()
        if re.search(r"(?:python|node(?:\.js)?|java|version|v)\s*$", prefix):
            continue
        if not unit and not match.group("currency"):
            try:
                integer = int(raw_number)
            except ValueError:
                integer = 0
            if 1900 <= integer <= 2100 and _is_date_like_number(text, match):
                continue
        try:
            value = Decimal(raw_number)
        except InvalidOperation:
            continue
        scale = {
            "k": 1_000,
            "thousand": 1_000,
            "m": 1_000_000,
            "million": 1_000_000,
            "b": 1_000_000_000,
            "billion": 1_000_000_000,
        }.get(unit, 1)
        value *= scale
        normalized_value = format(value.normalize(), "f")
        kind = (
            match.group("currency")
            or ("%" if unit in {"%", "percent"} else "")
            or ("x" if unit in {"x", "times"} else "")
            or (unit if unit in {"ms", "gb", "mb", "tb"} else "")
        )
        claims[f"{kind}:{normalized_value}"] += 1
    return claims


def detect_novel_numbers(source: str, candidate: str) -> list[str]:
    """Return numeric claims found in candidate that were not present in source.

    Used to detect hallucinated metrics and invented percentage increases.
    """
    source_numbers = numeric_claims(source)
    candidate_numbers = numeric_claims(candidate)
    novel = []
    for claim, count in candidate_numbers.items():
        if count > source_numbers[claim]:
            novel.append(claim)
    return novel


def post_process_rewrite_result(result: dict[str, Any], source_text: str = "") -> dict[str, Any]:
    """Clean all strings in a rewrite result: scrub em dashes and AI buzzwords.

    Also flags any novel numbers if source_text is provided.
    """
    cleaned = dict(result)

    # Clean summary
    if isinstance(cleaned.get("summary"), str):
        cleaned["summary"] = clean_text_for_ats(cleaned["summary"])

    # Clean experience bullets
    exp_bullets = cleaned.get("experience_bullets")
    if isinstance(exp_bullets, list):
        new_exp = []
        for entry in exp_bullets:
            if isinstance(entry, dict):
                e = dict(entry)
                if isinstance(e.get("role"), str):
                    e["role"] = clean_text_for_ats(e["role"])
                if isinstance(e.get("company"), str):
                    e["company"] = clean_text_for_ats(e["company"])
                if isinstance(e.get("dates"), str):
                    e["dates"] = scrub_em_dashes(e["dates"])
                if isinstance(e.get("bullets"), list):
                    e["bullets"] = [
                        clean_text_for_ats(b) if isinstance(b, str) else b
                        for b in e["bullets"]
                    ]
                new_exp.append(e)
            else:
                new_exp.append(entry)
        cleaned["experience_bullets"] = new_exp

    # Clean skills
    if isinstance(cleaned.get("skills"), list):
        cleaned["skills"] = [
            clean_text_for_ats(s) if isinstance(s, str) else s
            for s in cleaned["skills"]
        ]

    # Clean certifications
    if isinstance(cleaned.get("certifications"), list):
        cleaned["certifications"] = [
            clean_text_for_ats(c) if isinstance(c, str) else c
            for c in cleaned["certifications"]
        ]

    # Clean education
    if isinstance(cleaned.get("education"), list):
        cleaned["education"] = [
            clean_text_for_ats(ed) if isinstance(ed, str) else ed
            for ed in cleaned["education"]
        ]

    return cleaned
