"""Evidence-based vessel category and machinery terminology extraction."""
from __future__ import annotations

import re

from pipeline.maritime import (
    TANKER_TYPES, BULK_TYPES, CONTAINER_TYPES, OFFSHORE_TYPES, PASSENGER_TYPES,
)
from services.sea_service import _safe_evidence

_VOCAB = {
    'category': {
        'tanker': tuple(TANKER_TYPES)+('oil tankers','chemical tankers','gas tankers','lng carriers','lpg carriers'),
        'bulk_carrier': tuple(BULK_TYPES)+('bulk carriers','ore carriers'),
        'container': tuple(CONTAINER_TYPES)+('container vessels','container ships'),
        'offshore': tuple(OFFSHORE_TYPES)+('offshore vessels','offshore support vessels'),
        'passenger': tuple(PASSENGER_TYPES)+('passenger vessels','passenger ships','cruise ships','cruise vessels'),
        'general_cargo': ('general cargo vessel','cargo vessel','cargo ship'),
        'ferry': ('ferry','passenger ferry','ro-ro ferry'),
        'research': ('research vessel','survey vessel'),
        'yacht': ('yacht','superyacht'),
    },
    'propulsion_system': {
        'diesel_electric': ('diesel-electric','diesel electric propulsion'),
        'azipod': ('azipod','pod propulsion'), 'lng_dual_fuel': ('lng dual-fuel','dual fuel lng'),
        'steam': ('steam turbine propulsion',),
    },
    'engine_type': {
        'two_stroke_diesel': ('two-stroke diesel','2-stroke diesel','slow-speed diesel'),
        'four_stroke_diesel': ('four-stroke diesel','4-stroke diesel','medium-speed diesel'),
        'gas_turbine': ('gas turbine',), 'dual_fuel_engine': ('dual-fuel engine','dual fuel engine'),
    },
}


def vessel_vocabulary() -> dict:
    return {kind:{name:{'aliases':list(aliases),'source_type':'general_industry_terminology',
                        'specification_inferred':False}
                  for name,aliases in values.items()} for kind,values in _VOCAB.items()}


def extract_vessel_intelligence(text: str) -> list[dict]:
    results=[]; seen=set()
    for kind, values in _VOCAB.items():
        for canonical, aliases in values.items():
            for alias in sorted(aliases,key=len,reverse=True):
                for match in re.finditer(rf'(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])',text or '',re.I):
                    key=(kind,canonical,match.start())
                    if key in seen: continue
                    seen.add(key)
                    evidence=_safe_evidence((text or '')[max(0,match.start()-70):min(len(text),match.end()+70)].strip())
                    results.append({'entity_type':kind,'normalized':canonical,
                                    'surface':match.group(),'start':match.start(),'end':match.end(),
                                    'evidence':evidence,
                                    'source_type':'general_industry_terminology',
                                    'verification_status':'unverified_resume_claim',
                                    'specifications':{},
                                    'department_relevance':{'deck': kind=='category' and canonical in {'tanker','bulk_carrier','container','passenger','ferry','general_cargo'},
                                                            'engine':kind in {'propulsion_system','engine_type'},
                                                            'eto':kind=='propulsion_system'}})
    return sorted(results,key=lambda x:(x['start'],x['entity_type']))


def match_vessel_experience(candidate_entities: list[dict], requirements: list[dict] | list[str]) -> dict:
    needed={str(x.get('normalized') if isinstance(x,dict) else x).casefold() for x in requirements}
    found={str(x.get('normalized','')).casefold() for x in candidate_entities}
    satisfied=sorted(needed & found); missing=sorted(needed-found)
    return {'demonstrated':satisfied,'not_evidenced':missing,
            'status':'needs_review' if missing or satisfied else 'not_assessed',
            'evidence':[x for x in candidate_entities if x.get('normalized','').casefold() in needed],
            'note':'Vessel mentions are resume claims and do not establish vessel specifications or independent verification.'}
