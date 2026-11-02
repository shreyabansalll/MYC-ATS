"""Additive maritime entity evidence; deterministic extractors remain authoritative."""
from __future__ import annotations

import re
from functools import lru_cache

from rules.aliases import CERT_ALIASES, RANK_KEYWORDS
from pipeline.maritime import (
    TANKER_TYPES, BULK_TYPES, CONTAINER_TYPES, OFFSHORE_TYPES, PASSENGER_TYPES,
    detect_rank, detect_vessel_types, extract_certificates,
)

_PII = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|\b(?:passport|cdc|indos|document|certificate)\s*(?:no\.?\s*:|number\s*:|#|:)\s*[A-Z0-9/-]+|\b\d{7,}\b", re.I)
_VESSEL_GROUPS = {
    'tanker': TANKER_TYPES, 'bulk_carrier': BULK_TYPES,
    'container': CONTAINER_TYPES, 'offshore': OFFSHORE_TYPES,
    'passenger': PASSENGER_TYPES,
}


def _safe(text: str) -> str:
    return _PII.sub('[REDACTED]', text)[:240]


@lru_cache(maxsize=1)
def _matcher():
    try:
        import spacy
        from spacy.matcher import PhraseMatcher
        nlp = spacy.blank('en')
        ruler = nlp.add_pipe('entity_ruler', config={'overwrite_ents': True})
        patterns = []
        for rank, aliases in RANK_KEYWORDS.items():
            patterns.extend({'label':'MARITIME_RANK','pattern':a.strip()} for a in aliases if a.strip())
        for cert, aliases in CERT_ALIASES.items():
            patterns.extend({'label':'MARITIME_CERT','pattern':a.strip()} for a in aliases if a.strip())
        for group, aliases in _VESSEL_GROUPS.items():
            patterns.extend({'label':'VESSEL_TYPE','pattern':a.strip()} for a in aliases if a.strip())
        ruler.add_patterns(patterns)
        phrase = PhraseMatcher(nlp.vocab, attr='LOWER')
        for group, aliases in _VESSEL_GROUPS.items():
            phrase.add(group.upper(), [nlp.make_doc(alias) for alias in aliases if alias.strip()])
        return nlp, phrase
    except Exception:
        return None, None


def extract_maritime_entities(text: str) -> list[dict]:
    """Return normalized entities with source evidence and conservative verification labels."""
    if not isinstance(text, str) or not text.strip():
        return []
    out, seen = [], set()

    def add(kind, normalized, surface, start, end, method, status='needs_verification'):
        key = (kind, normalized.casefold(), start)
        if key in seen:
            return
        seen.add(key)
        out.append({'entity_type':kind,'normalized':normalized,'surface':surface,
                    'evidence':_safe(text[max(0,start-80):min(len(text),end+80)]),
                    'start':start,'end':end,'method':method,
                    'confidence':'high' if method in ('deterministic_rule','entity_ruler','phrase_matcher') else 'review',
                    'verification_status':status})

    # Existing deterministic methods remain source of truth; retain the first supporting mention.
    rank = detect_rank(text)
    if rank and rank != 'unknown':
        aliases = RANK_KEYWORDS.get(rank, [rank])
        hit = next((re.search(re.escape(a.strip()), text, re.I) for a in aliases if a.strip() and re.search(re.escape(a.strip()), text, re.I)), None)
        if hit: add('rank', rank, hit.group(), hit.start(), hit.end(), 'deterministic_rule')
    for cert in extract_certificates(text):
        aliases = CERT_ALIASES.get(cert, [cert])
        hit = next((re.search(re.escape(a), text, re.I) for a in aliases if re.search(re.escape(a), text, re.I)), None)
        if hit: add('certificate', cert, hit.group(), hit.start(), hit.end(), 'deterministic_rule')
    for vessel in detect_vessel_types(text):
        hit = re.search(re.escape(vessel), text, re.I)
        if hit: add('vessel_type', vessel, hit.group(), hit.start(), hit.end(), 'deterministic_rule')

    nlp, phrase = _matcher()
    if nlp:
        doc = nlp(text)
        for ent in doc.ents:
            if ent.label_ == 'VESSEL_TYPE':
                canonical = next((g for g, aliases in _VESSEL_GROUPS.items() if ent.text.casefold() in {x.casefold() for x in aliases}), ent.text.casefold())
                add('vessel_type', canonical, ent.text, ent.start_char, ent.end_char, 'entity_ruler')
        for match_id, start, end in phrase(doc):
            span = doc[start:end]
            canonical = nlp.vocab.strings[match_id].casefold()
            add('vessel_type', canonical, span.text, span.start_char, span.end_char, 'phrase_matcher')
    return out
