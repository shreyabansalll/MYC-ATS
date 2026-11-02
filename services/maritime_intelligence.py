"""Composable maritime candidate intelligence used by scoring and comparison flows."""
from __future__ import annotations

from pipeline.maritime import extract_certificates
from services.maritime_certificates import certificate_requirement_resolution
from services.rank_intelligence import evaluate_rank_suitability
from services.sea_service import calculate_sea_service
from services.vessel_intelligence import extract_vessel_intelligence, match_vessel_experience


def analyze_maritime_candidate(raw_text: str, rank: str, vacancy: dict | None = None) -> dict:
    vacancy=vacancy or {}
    service=calculate_sea_service(raw_text, vacancy=vacancy)
    rank_months={}
    for key,value in service['aggregates'].items():
        if key.startswith('rank:'):
            rank_months[key[5:].replace(' ','_')]=value['service_months']
    service['rank_months']=rank_months
    vessels=extract_vessel_intelligence(raw_text)
    certificate_resolution=certificate_requirement_resolution(rank,raw_text)
    cert_states={item['certificate']:item['status'] for item in certificate_resolution['items']}
    candidate={'rank':rank,'certificates':extract_certificates(raw_text),
               'certificate_evidence':cert_states,'raw_text':raw_text}
    rank_fit=evaluate_rank_suitability(candidate,vacancy,service=service)
    vessel_needs=vacancy.get('vessel_types') or []
    vessel_fit=match_vessel_experience(vessels,vessel_needs)
    return {
        'rank':rank_fit,
        'certificates':certificate_resolution,
        'sea_service':service,
        'vessel_experience':vessel_fit,
        'vessel_entities':vessels,
        'source_policy':'MYC operational rules are labeled as placement guidance; resume claims remain unverified.',
    }
