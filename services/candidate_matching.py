"""Explainable, separate eligibility and lexicographic candidate comparison."""
from __future__ import annotations


def compare_candidate_results(candidates: list[dict]) -> dict:
    """Rank by transparent tie-break factors; eligibility is not part of ranking."""
    rows=[]
    for item in candidates:
        vacancy=item.get('vacancy_match') or {}
        intel=item.get('maritime_intelligence') or {}
        rank=intel.get('rank') or {}
        vessel=intel.get('vessel_experience') or {}
        semantic=(item.get('semantic_match') or {}).get('semantic_score')
        gaps=vacancy.get('skill_gaps') or {}
        demonstrated=len(gaps.get('demonstrated') or [])
        certs=sum(1 for req in vacancy.get('mandatory_requirements', [])
                 if req.get('type')=='certificate' and req.get('status')=='demonstrated')
        relevant=(intel.get('sea_service') or {}).get('vacancy_relevant') or {}
        rows.append({**item,'comparison_factors':{
            'demonstrated_vacancy_requirements':demonstrated,
            'demonstrated_mandatory_certificates':certs,
            'rank_match':rank.get('rank_match'),
            'vacancy_relevant_service_months':relevant.get('service_months',0),
            'demonstrated_vessel_categories':len(vessel.get('demonstrated') or []),
            'generic_ats_score':item.get('ats_score'),
            'vacancy_match_score':vacancy.get('vacancy_match_score'),
            'semantic_similarity':semantic,
        },'eligibility':rank.get('status','needs_manual_review')})
    # Eligibility intentionally does not appear in this ranking tuple.
    rows.sort(key=lambda x:(
        x['comparison_factors']['demonstrated_vacancy_requirements'],
        x['comparison_factors']['demonstrated_mandatory_certificates'],
        x['comparison_factors']['rank_match'] is True,
        x['comparison_factors']['vacancy_relevant_service_months'],
        x['comparison_factors']['demonstrated_vessel_categories'],
        x['comparison_factors']['generic_ats_score'] or 0,
    ),reverse=True)
    for position,row in enumerate(rows,1): row['comparison_position']=position
    return {'ranking_method':'lexicographic evidence comparison; no combined score; eligibility is reported separately',
            'eligibility_is_separate_from_ranking':True,'candidates':rows,
            'note':'Ordering summarizes supplied evidence and is not a hiring recommendation. Resume claims remain unverified.'}
