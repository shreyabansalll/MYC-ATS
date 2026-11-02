"""Rank and eligibility reasoning; progressions are explicitly MYC guidance."""
from __future__ import annotations

import re
from dataclasses import dataclass
from services.maritime_certificates import resolve_certificate_mentions, normalize_certificate_name
from rules.cert_rules import CERT_MATRIX, cert_requirements_for_rank, normalize_cert_rank
from services.sea_service import calculate_sea_service


@dataclass(frozen=True)
class RankNode:
    rank: str
    department: str
    certificate_matrix_rank: str
    source: str
    progression_policy: str


# Departmental sequence is a candidate-search aid, not an assertion that promotion
# is universally permitted. ETO placement guidance is explicitly MYC policy.
_SEQUENCES={
    'deck':('master','chief_officer','second_officer','third_officer','junior_officer','deck_cadet','bosun','able_seaman','ordinary_seaman'),
    'engine':('chief_engineer','first_engineer','second_engineer','third_engineer','fourth_engineer','engine_cadet','engine_rating'),
    'eto':('chief_eto','first_eto','second_eto','third_eto','electrical_cadet'),
}
_SOURCE='Derived from MYC Certifications Resume.xlsx rank/dept lists; progression is a search heuristic, not a regulatory ladder.'
RANK_ONTOLOGY={rank:RankNode(rank,entry['department'],rank,entry['source'],_SOURCE)
               for rank,entry in CERT_MATRIX.items()}


def normalize_rank(value: str) -> str | None:
    raw=(value or '').strip().casefold().replace('-',' ')
    slug=normalize_cert_rank(raw)
    if slug in CERT_MATRIX:return slug
    return None


def rank_progression(current_rank: str, previous_ranks: list[str] | None = None) -> dict:
    current=normalize_rank(current_rank)
    history=[normalize_rank(x) for x in previous_ranks or []]
    history=[x for x in history if x]
    if not current:
        return {'current_rank':None,'previous_ranks':history,'department':None,'potential_next_ranks':[],
                'progression_basis':'unknown','status':'needs_review'}
    department=CERT_MATRIX[current]['department']
    sequence=next((values for key,values in _SEQUENCES.items() if key==department),())
    # Only offer adjacent candidates from the internal search sequence; all remain human-reviewed.
    possible=[]
    if current in sequence:
        index=sequence.index(current)
        if index>0: possible.append(sequence[index-1])
    return {'current_rank':current,'previous_ranks':history,'department':department,
            'potential_next_ranks':possible,'progression_basis':_SOURCE,
            'status':'potential' if possible else 'needs_review',
            'promotion_path_universal':False}


def evaluate_rank_suitability(candidate: dict, vacancy: dict, *, service: dict | None = None) -> dict:
    """Return separate verified/potential/missing/ineligible/review signals."""
    current=normalize_rank(candidate.get('rank') or '')
    target=normalize_rank(vacancy.get('rank') or '')
    certs=candidate.get('certificates') or []
    cert_states=candidate.get('certificate_evidence') or {}
    mentions=resolve_certificate_mentions(candidate.get('raw_text') or '')
    service=service or {}
    states={}
    cert_needs=[item.get('text') for item in vacancy.get('structured_requirements', [])
                if item.get('type')=='certificate' and item.get('priority')=='mandatory']
    if not cert_needs:
        cert_needs=vacancy.get('required_certificates') or []
    for cert in cert_needs:
        matched=any(str(x).casefold() in str(cert).casefold() or str(cert).casefold() in str(x).casefold() for x in certs)
        key=normalize_certificate_name(cert)
        related=[m for m in mentions if any(key in value or value in key for value in m['identity_candidates'])]
        if any(m['status']=='conflict' and re.search(r'\b(?:not held|do not have|does not have|never obtained)\b',m.get('evidence',''),re.I) for m in related):
            state='confirmed_missing'
        elif any(m['status']=='conflict' and re.search(r'\b(?:expired|lapsed)\b',m.get('evidence',''),re.I) for m in related):
            state='expired'
        elif any(m['status']=='evidenced' for m in related) or (not related and matched): state='evidenced_unverified'
        else: state='not_evidenced'
        states[cert]=cert_states.get(cert,state)
    explicit_missing=[c for c,v in states.items() if v=='confirmed_missing']
    tenure=service.get('rank_months',{}).get(current) if current else None
    duration_requirements=[item for item in vacancy.get('structured_requirements', [])
                           if item.get('type')=='experience' and item.get('priority')=='mandatory']
    minimum_months=[]
    for item in duration_requirements:
        match=re.search(r'(\d+(?:\.\d+)?)\s*(years?|months?)',item.get('text',''),re.I)
        if match:
            amount=float(match.group(1)); minimum_months.append(amount*12 if match.group(2).casefold().startswith('year') else amount)
    required_months=max(minimum_months) if minimum_months else None
    relevant_months=(service.get('vacancy_relevant') or {}).get('service_months')
    service_evaluation={'required_months':required_months,'claimed_relevant_months':relevant_months,
                        'status':'not_required' if required_months is None else
                                 'potentially_meets' if relevant_months is not None and relevant_months>=required_months else
                                 'insufficient_or_missing_evidence',
                        'verification_status':'unverified_resume_claim'}
    if explicit_missing:status='ineligible_confirmed_requirement'
    elif not current or not target or tenure is None:status='needs_manual_review'
    elif current==target and service_evaluation['status'] in {'not_required','potentially_meets'}:status='potentially_eligible'
    else:status='needs_manual_review'
    return {'status':status,'current_rank':current,'target_rank':target,
            'rank_match':current==target if current and target else None,
            'rank_experience_months':tenure,'certificate_evidence':states,
            'sea_service_requirement':service_evaluation,
            'missing_evidence':[c for c,s in states.items() if s=='not_evidenced'],
            'confirmed_ineligible_requirements':explicit_missing,
            'policy_source':'MYC rank lists and rules/cert_rules.py; not a regulatory determination',
            'note':'No promotion pathway is assumed. Resume evidence is unverified.'}
