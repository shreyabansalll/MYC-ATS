from datetime import date

from services.candidate_matching import compare_candidate_results
from services.maritime_certificates import (
    CERTIFICATE_ONTOLOGY, certificate_requirement_resolution,
    resolve_certificate_mentions,
)
from services.rank_intelligence import evaluate_rank_suitability, rank_progression
from services.sea_service import calculate_sea_service
from services.vessel_intelligence import extract_vessel_intelligence, match_vessel_experience
from services.vacancy_match import extract_vacancy_requirements, match_vacancy


def test_certificate_ontology_preserves_source_and_does_not_infer_equivalence():
    assert CERTIFICATE_ONTOLOGY
    definition=next(value for value in CERTIFICATE_ONTOLOGY.values() if value.official_name.startswith('ECDIS'))
    assert definition.source.startswith('MYC Certifications Resume.xlsx')
    assert definition.validity_basis.startswith('No certificate duration')
    mentions=resolve_certificate_mentions('GMDSS GOC')
    assert mentions and all(item['equivalence_inferred'] is False for item in mentions)
    conflict=resolve_certificate_mentions('GMDSS not held')
    assert any(item['status']=='conflict' for item in conflict)


def test_sea_service_merges_overlap_deduplicates_and_aggregates_by_rank_vessel():
    text=(
        'Third Officer | M/V Blue Horizon | Company: Example | 01/2020 - 12/2020\n'
        'Third Officer | M/V Blue Horizon | Company: Example | 01/2020 - 12/2020\n'
        'Third Officer | M/V Blue Horizon | Company: Example | 07/2020 - 06/2021\n'
    )
    result=calculate_sea_service(text,today=date(2024,1,1),
                                 verified_records=[{'start_date':'2020-01-01','end_date':'2020-06-30'}])
    assert len(result['periods'])==2
    assert len(result['duplicates'])==1
    assert result['overlaps'][0]['overlap_days']==184
    assert result['periods'][0]['verification_status']=='partially_verified'
    assert result['total']['service_days']==547  # Jan 2020 through Jun 2021, overlap counted once
    assert result['total']['verified_days']==182
    assert result['total']['unverified_days']==365
    assert '01/2020 - 12/2020' in result['periods'][0]['evidence']
    assert result['aggregates']['department:deck']['service_days']==547
    assert result['aggregates']['rank:third officer']['service_days']==547
    assert result['aggregates']['vessel:Blue Horizon']['service_days']==547
    assert result['total']['unverified_months'] > 0


def test_sea_service_marks_ambiguous_day_dates_and_handles_partial_months():
    result=calculate_sea_service('Third Officer 01/02/2022 - 03/04/2022',today=date(2024,1,1))
    assert result['periods'][0]['date_ambiguous'] is True
    partial=calculate_sea_service('Third Officer Jan 2022 - Mar 2022',today=date(2024,1,1))
    assert partial['periods'][0]['date_precision']=='partial_month'
    assert partial['total']['service_months'] >= 2.9


def test_vessel_intelligence_extracts_only_named_properties_and_matches_category():
    entities=extract_vessel_intelligence('Served on a passenger vessel with diesel-electric propulsion and a MAN B&W engine.')
    assert {x['normalized'] for x in entities} >= {'passenger','diesel_electric'}
    assert all(x['specifications']=={} for x in entities)
    assert match_vessel_experience(entities,['passenger'])['demonstrated']==['passenger']


def test_vacancy_profile_categorizes_requirements_and_surfaces_unresolved_items():
    profile=extract_vacancy_requirements(
        'Third Officer\nRequired:\nGMDSS, ECDIS, 24 months sea service\n'
        'Preferred:\nBridge watchkeeping\nMust meet company-specific endorsement X9'
    )
    structured=profile['structured_requirements']
    assert any(x['type']=='rank' and x['priority']=='mandatory' for x in structured)
    assert any(x['type']=='certificate' and x['priority']=='mandatory' for x in structured)
    assert any(x['type']=='experience' for x in structured)
    assert profile['unresolved']
    match=match_vacancy('Third Officer\nRequired:\nGMDSS', 'Third Officer\nGMDSS GOC')
    assert match['mandatory_requirements'][0]['status']=='demonstrated'


def test_rank_engine_does_not_treat_progression_as_universal_or_claim_verified_eligibility():
    progression=rank_progression('third officer')
    assert progression['potential_next_ranks']==['second_officer']
    assert progression['promotion_path_universal'] is False
    decision=evaluate_rank_suitability({'rank':'third officer','certificates':[]},
                                       {'rank':'third officer','structured_requirements':[]})
    assert decision['status']=='needs_manual_review'
    assert decision['rank_match'] is True
    assert decision['note'].endswith('Resume evidence is unverified.')


def test_candidate_comparison_keeps_eligibility_separate_from_explainable_ordering():
    report=compare_candidate_results([
        {'candidate_id':'A','ats_score':70,'vacancy_match':{'skill_gaps':{'demonstrated':['GMDSS']}},
         'maritime_intelligence':{'rank':{'status':'ineligible_confirmed_requirement'},
          'sea_service':{'vacancy_relevant':{'service_months':30}},'vessel_experience':{'demonstrated':['tanker']}}},
        {'candidate_id':'B','ats_score':80,'vacancy_match':{'skill_gaps':{'demonstrated':[]}},
         'maritime_intelligence':{'rank':{'status':'potentially_eligible'},
          'sea_service':{'vacancy_relevant':{'service_months':5}},'vessel_experience':{'demonstrated':[]}}},
    ])
    assert report['eligibility_is_separate_from_ranking'] is True
    assert report['candidates'][0]['candidate_id']=='A'
    assert report['candidates'][0]['eligibility']=='ineligible_confirmed_requirement'


def test_rank_evaluation_checks_required_service_without_disqualifying_unverified_claims():
    vacancy={'rank':'third officer','structured_requirements':[
        {'type':'experience','priority':'mandatory','text':'24 months sea service'}]}
    enough=evaluate_rank_suitability({'rank':'third officer','certificates':[]},vacancy,
        service={'rank_months':{'third_officer':24},'vacancy_relevant':{'service_months':24}})
    short=evaluate_rank_suitability({'rank':'third officer','certificates':[]},vacancy,
        service={'rank_months':{'third_officer':12},'vacancy_relevant':{'service_months':12}})
    assert enough['status']=='potentially_eligible'
    assert enough['sea_service_requirement']['status']=='potentially_meets'
    assert short['status']=='needs_manual_review'
    assert short['confirmed_ineligible_requirements']==[]


def test_rank_engine_flags_only_explicitly_missing_vacancy_certificate():
    vacancy={'rank':'third officer','structured_requirements':[
        {'type':'certificate','priority':'mandatory','text':'GMDSS (GOC or ROC)'}]}
    absent=evaluate_rank_suitability({'rank':'third officer','certificates':[],'raw_text':'Third Officer'},vacancy)
    missing=evaluate_rank_suitability({'rank':'third officer','certificates':[],'raw_text':'Third Officer\nGMDSS not held'},vacancy)
    assert absent['status']=='needs_manual_review'
    assert absent['missing_evidence']==['GMDSS (GOC or ROC)']
    assert missing['status']=='ineligible_confirmed_requirement'
    assert missing['confirmed_ineligible_requirements']==['GMDSS (GOC or ROC)']
