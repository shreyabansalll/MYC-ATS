"""Evidence-first certificate ontology derived from MYC's existing rule registry.

It records MYC placement classification separately from regulatory references and
does not infer equivalence between similar certificate names.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from rules.aliases import CERT_ALIASES
from rules.cert_rules import CERT_MATRIX, CERT_UNIVERSAL_REQUIREMENTS, _aliases_for_cert

_VALIDITY_NOTES = {
    'yellow fever vaccination certificate': (
        'MYC spreadsheet states lifelong validity; trade applicability is conditional.',
        'Certifications Resume.xlsx',
    ),
    'passport': (
        'MYC keyword PDFs state at least 12 months remaining for cruise applications; confirm per employer portal.',
        'Key words Officers.pdf; Keyword Engineers and ETO (1).pdf',
    ),
}


def normalize_certificate_name(value: str) -> str:
    return re.sub(r'[^a-z0-9]+', ' ', (value or '').casefold()).strip()


def _safe_evidence(value: str) -> str:
    value=re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '[redacted email]', value[:240])
    value=re.sub(r'(?<!\w)\+?\d[\d\s()./-]{7,}\d(?!\w)',
                 lambda m:'[redacted number]' if sum(c.isdigit() for c in m.group())>=10 else m.group(),value)
    return re.sub(r'\b(passport|cdc|indos|document|certificate)\s*(?:no\.?\s*:|number\s*:|#|:)\s*[A-Z0-9/-]+', r'\1 [redacted]', value, flags=re.I)


@dataclass(frozen=True)
class CertificateDefinition:
    identity: str
    official_name: str
    aliases: tuple[str, ...]
    category: str
    departments: tuple[str, ...]
    ranks: tuple[str, ...]
    classification: str
    evidence_requirements: tuple[str, ...]
    validity_basis: str
    regulatory_reference: str | None
    source: str
    relationships: tuple[dict, ...]


def _definitions() -> dict[str, CertificateDefinition]:
    collected: dict[str, dict] = {}
    for rank, entry in CERT_MATRIX.items():
        for field, classification in (
            ('mandatory_certs', 'mandatory'), ('preferred_certs', 'optional'),
            ('conditional_certs', 'conditional'),
        ):
            for name in entry.get(field, []):
                key = normalize_certificate_name(name)
                item = collected.setdefault(key, {
                    'name': name, 'departments': set(), 'ranks': set(), 'classes': set(),
                    'regulatory_reference': None,
                })
                item['departments'].add(entry['department'])
                item['ranks'].add(rank)
                item['classes'].add(classification)
                reference = entry.get('regulatory_reference')
                if reference and (name.casefold().find('stcw') >= 0 or key.find('stcw') >= 0):
                    item['regulatory_reference'] = reference
    for name in CERT_UNIVERSAL_REQUIREMENTS:
        key = normalize_certificate_name(name)
        collected.setdefault(key, {
            'name': name, 'departments': {'deck','engine','eto','electrical'},
            'ranks': set(CERT_MATRIX), 'classes': {'mandatory'},
            'regulatory_reference': None,
        })
    result = {}
    source = 'MYC Certifications Resume.xlsx; aliases from rules/aliases.py'
    for key, item in collected.items():
        name = item['name']
        aliases = set(_aliases_for_cert(name))
        aliases.update(CERT_ALIASES.get(name, []))
        aliases.add(name)
        stcw = re.search(r'STCW\s+((?:II|III)/\d+)', name, re.I)
        category = (
            'competency' if re.search(r'coc|certificate of competency', name, re.I) else
            'proficiency' if re.search(r'cop|certificate of proficiency|watch', name, re.I) else
            'medical' if re.search(r'medical|eng1', name, re.I) else
            'safety_training' if re.search(r'fire|survival|safety|pscrb|stsdsd', name, re.I) else
            'identity_document' if re.search(r'passport|cdc|indos|identity|sid', name, re.I) else
            'endorsement_or_course' if re.search(r'tanker|dce|ecdis|gmdss|arpa|brm|btm', name, re.I) else
            'other'
        )
        validity, validity_source = _VALIDITY_NOTES.get(
            key,
            ('No certificate duration is specified in the supplied matrix; use an explicit printed expiry date only.', None),
        )
        item_source = source if validity_source is None else f'{source}; validity policy: {validity_source}'
        relationships=()
        if 'security training for seafarers' in key:
            relationships=({'relationship':'MYC_policy_alternative',
                            'target':'Ship Security Officer (SSO) course or Security Awareness',
                            'source':item_source,'equivalence':False},)
        result[key] = CertificateDefinition(
            identity=key,
            official_name=name,
            aliases=tuple(sorted((a for a in aliases if a), key=str.casefold)),
            category=category,
            departments=tuple(sorted(item['departments'])),
            ranks=tuple(sorted(item['ranks'])),
            classification=(next(iter(item['classes'])) if len(item['classes']) == 1 else 'context_dependent'),
            evidence_requirements=('explicit certificate name or recognized alias in source text',),
            validity_basis=validity,
            regulatory_reference=f"STCW {stcw.group(1)}" if stcw else item['regulatory_reference'],
            source=item_source,
            relationships=relationships,
        )
    return result


CERTIFICATE_ONTOLOGY = _definitions()

def certificate_ontology_dict() -> list[dict]:
    """JSON-safe ontology export for diagnostics and downstream clients."""
    return [asdict(value) for _, value in sorted(CERTIFICATE_ONTOLOGY.items())]


def resolve_certificate_mentions(text: str) -> list[dict]:
    """Resolve textual mentions; ambiguous aliases are surfaced, never guessed."""
    source = text or ''
    hits: dict[tuple[int, int], set[str]] = {}
    surfaces: dict[tuple[int, int], str] = {}
    for key, definition in CERTIFICATE_ONTOLOGY.items():
        for alias in definition.aliases:
            if len(alias.strip()) < 3:
                continue
            pattern = re.compile(rf'(?<![A-Za-z0-9]){re.escape(alias.strip())}(?![A-Za-z0-9])', re.I)
            for match in pattern.finditer(source):
                span = (match.start(), match.end())
                hits.setdefault(span, set()).add(key)
                surfaces[span] = match.group(0)
    results = []
    for (start, end), keys in sorted(hits.items()):
        names = sorted(CERTIFICATE_ONTOLOGY[key].official_name for key in keys)
        conflict = any(re.search(rf'\b(?:not held|do not have|does not have|never obtained|expired|lapsed)\b', source[max(0,start-48):min(len(source),end+48)], re.I) for _ in (0,))
        results.append({
            'identity_candidates': [normalize_certificate_name(name) for name in names],
            'official_name': names[0] if len(names) == 1 else None,
            'surface': surfaces[(start,end)], 'start': start, 'end': end,
            'evidence': _safe_evidence(source[max(0,start-80):min(len(source),end+80)]),
            'status': 'ambiguous' if len(names) > 1 else 'conflict' if conflict else 'evidenced',
            'verification_status': 'unverified_resume_claim',
            'equivalence_inferred': False,
        })
    return results


def certificate_requirement_resolution(rank: str, resume_text: str) -> dict:
    """Resolve rank requirements against resume mentions with explicit statuses."""
    normalized = (rank or '').strip().casefold().replace(' ', '_')
    entry = CERT_MATRIX.get(normalized, {})
    mentions = resolve_certificate_mentions(resume_text)
    evidenced = {normalize_certificate_name(m['official_name']) for m in mentions if m['status'] == 'evidenced' and m['official_name']}
    items = []
    for field, classification in (('mandatory_certs','mandatory'),('preferred_certs','optional'),('conditional_certs','conditional')):
        for name in entry.get(field, []):
            key = normalize_certificate_name(name)
            found = key in evidenced or any(
                key in candidate or candidate in key
                for m in mentions if m['status'] == 'evidenced'
                for candidate in m['identity_candidates']
            )
            related=[m for m in mentions if any(key in candidate or candidate in key for candidate in m['identity_candidates'])]
            explicitly_absent=any(m['status']=='conflict' and re.search(r'\b(?:not held|do not have|does not have|never obtained)\b',m['evidence'],re.I) for m in related)
            expired=any(m['status']=='conflict' and re.search(r'\b(?:expired|lapsed)\b',m['evidence'],re.I) for m in related)
            items.append({
                'certificate': name, 'classification': classification,
                'status': 'expired' if expired else 'confirmed_missing' if explicitly_absent else 'evidenced_unverified' if found else 'not_evidenced',
                'evidence': next((m['evidence'] for m in related), None),
                'source': entry.get('source'), 'rule_type': entry.get('rule_type'),
                'regulatory_reference': entry.get('regulatory_reference'),
                'equivalence_inferred': False,
            })
    return {'rank': normalized or None, 'items': items, 'mentions': mentions,
            'conflicts': [m for m in mentions if m['status'] in {'ambiguous','conflict'}],
            'eligibility_decision': 'requires_review',
            'note': 'Resume mentions are unverified evidence. Similarity does not establish regulatory equivalence.'}
