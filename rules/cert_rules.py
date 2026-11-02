# rules/cert_rules.py
# Rank -> required certificates and rank -> minimum sea service (months).
# Externalized from pipeline/maritime.py — this is a lookup-table
# problem (~20 ranks, ~5 cert types), not logic, so it lives as data here.
# Plain Python dicts for now; move to YAML/JSON only if these start being
# edited by non-engineers.

from __future__ import annotations

import re
import calendar
from datetime import date, datetime
from enum import Enum

CERT_REQUIREMENTS = {
    'master':           ['CoC', 'STCW', 'GMDSS', 'ENG1'],
    'chief officer':    ['CoC', 'STCW', 'GMDSS', 'ENG1'],
    'second officer':   ['CoC', 'STCW', 'GMDSS', 'ENG1'],
    '2nd officer':      ['CoC', 'STCW', 'GMDSS', 'ENG1'],
    'third officer':    ['CoC', 'STCW', 'ENG1'],
    '3rd officer':      ['CoC', 'STCW', 'ENG1'],
    'deck cadet':       ['STCW', 'ENG1'],
    'engine cadet':     ['STCW', 'ENG1'],
    'electrical cadet': ['STCW', 'ENG1'],  # mirrors deck cadet/engine cadet exactly -- see rules/aliases.py
    'chief engineer':   ['CoC', 'STCW', 'ENG1'],
    'second engineer':  ['CoC', 'STCW', 'ENG1'],
    '2nd engineer':     ['CoC', 'STCW', 'ENG1'],
    'third engineer':   ['CoC', 'STCW', 'ENG1'],
    '3rd engineer':     ['CoC', 'STCW', 'ENG1'],
    'fourth engineer':  ['CoC', 'STCW', 'ENG1'],
    'eto':              ['CoC', 'STCW', 'ENG1'],
    'electro technical officer': ['CoC', 'STCW', 'ENG1'],
    'bosun':            ['STCW', 'ENG1'],
    'ab seaman':        ['STCW', 'ENG1'],
    'able seaman':      ['STCW', 'ENG1'],
    'motorman':         ['STCW', 'ENG1'],
    'fitter':           ['STCW', 'ENG1'],
    'oiler':            ['STCW', 'ENG1'],
    'jwko':             ['STCW', 'ENG1'],
}

SEA_SERVICE_MIN = {
    'master':           36,
    'chief officer':    24,
    'second officer':   12,
    '2nd officer':      12,
    'third officer':    6,
    '3rd officer':      6,
    'deck cadet':       0,
    'engine cadet':     0,
    'electrical cadet': 0,
    'chief engineer':   24,
    'second engineer':  12,
    '2nd engineer':     12,
    'third engineer':   6,
    '3rd engineer':     6,
    'fourth engineer':  6,
    'eto':              12,
    'electro technical officer': 12,
    'bosun':            24,
    'ab seaman':        12,
    'able seaman':      12,
    'motorman':         12,
    'fitter':           12,
    'oiler':            12,
    'jwko':             6,
}


class CertEvidence(str, Enum):
    EVIDENCED = 'evidenced'
    NOT_EVIDENCED = 'not_evidenced'
    CONFIRMED_MISSING = 'confirmed_missing'
    EXPIRED = 'expired'
    NOT_APPLICABLE = 'not_applicable'


CERT_MATRIX_SOURCE = 'Certifications Resume.xlsx — MYC operational reference'
CERT_UNIVERSAL_REQUIREMENTS = [
    'Passport',
    'CDC (Continuous Discharge Certificate)',
    'COP / COC',
    'INDoS Number',
    "Seafarer's Identity Document (SID)",
    'Medical Fitness Certificate',
    'STCW Basic Safety Training (BST)',
    'Proficiency in Survival Craft and Rescue Boats (PSCRB)',
    'Security Training for Seafarers with Designated Security Duties (STSDSD)',
    'Yellow Fever vaccination certificate',
    'COVID-19 vaccination evidence',
    'Police Clearance Certificate',
    'Experience letters / appraisals / certificates of discharge',
]
CERT_UNIVERSAL_CONDITIONS = {
    "Seafarer's Identity Document (SID)": 'Where applicable; compulsory for Form 1 foreign-going vessels.',
    'Yellow Fever vaccination certificate': 'Where the vessel trades to affected regions.',
    'COVID-19 vaccination evidence': 'When required by the vessel, itinerary, or employer.',
    'Police Clearance Certificate': 'Often company-required; confirm for the target placement.',
}


def _matrix_entry(
    department: str,
    stcw_code: str | None,
    mandatory_certs: list[str],
    preferred_certs: list[str] | None = None,
    conditional_certs: list[str] | None = None,
) -> dict:
    return {
        'department': department,
        'stcw_code': stcw_code,
        'source': CERT_MATRIX_SOURCE,
        'rule_type': 'myc_placement_standard',
        'regulatory_reference': f'STCW {stcw_code}' if stcw_code else None,
        'mandatory_certs': mandatory_certs,
        'preferred_certs': preferred_certs or [],
        'conditional_certs': conditional_certs or [],
        'universal_required': True,
    }


CERT_MATRIX = {
    'master': _matrix_entry(
        'deck', 'II/2',
        [
            'Certificate of Competency (COC) — Master (STCW II/2)',
            'ECDIS (generic and type-specific)',
            'GMDSS (GOC or ROC)',
            'Bridge Resource Management / Bridge Team Management (BRM / BTM)',
            'Medical Care',
        ],
        ['ARPA (Automatic Radar Plotting Aid)'],
        ['Oil / Chemical / Gas Tanker Familiarization and Dangerous Cargo Endorsement (DCE)',
         'Passenger Ship Safety / Crowd and Crisis Management / Passenger Familiarization'],
    ),
    'chief_officer': _matrix_entry(
        'deck', 'II/2',
        [
            'Certificate of Competency (COC) — Chief Mate (STCW II/2)',
            'ECDIS (generic and type-specific)',
            'GMDSS (GOC or ROC)',
            'Bridge Resource Management / Bridge Team Management (BRM / BTM)',
        ],
        ['ARPA (Automatic Radar Plotting Aid)', 'Medical Care'],
        ['Oil / Chemical / Gas Tanker Familiarization and Dangerous Cargo Endorsement (DCE)',
         'Passenger Ship Safety / Crowd and Crisis Management / Passenger Familiarization'],
    ),
    'second_officer': _matrix_entry(
        'deck', 'II/1',
        [
            'Certificate of Competency (COC) — OOW (STCW II/1)',
            'ECDIS (generic and type-specific)',
            'GMDSS (GOC or ROC)',
        ],
        ['ARPA (Automatic Radar Plotting Aid)',
         'Bridge Resource Management / Bridge Team Management (BRM / BTM)'],
        ['Passenger Ship Safety / Crowd and Crisis Management / Passenger Familiarization'],
    ),
    'third_officer': _matrix_entry(
        'deck', 'II/1',
        [
            'Certificate of Competency (COC) — OOW (STCW II/1)',
            'ECDIS (generic and type-specific)',
            'GMDSS (GOC or ROC)',
        ],
        ['ARPA (Automatic Radar Plotting Aid)'],
        ['Passenger Ship Safety / Crowd and Crisis Management / Passenger Familiarization'],
    ),
    'junior_officer': _matrix_entry(
        'deck', 'II/1',
        [
            'Certificate of Competency (COC) — OOW (STCW II/1)',
            'ECDIS (generic and type-specific)',
            'GMDSS (GOC or ROC)',
        ],
        ['ARPA (Automatic Radar Plotting Aid)'],
        ['Passenger Ship Safety / Crowd and Crisis Management / Passenger Familiarization'],
    ),
    'deck_cadet': _matrix_entry(
        'deck', 'II/4',
        [
            'Pre-Sea Training completion certificate (nautical course as applicable)',
            'STCW Basic Safety Training (BST)',
            'Proficiency in Survival Craft and Rescue Boats (PSCRB)',
            'Security Training for Seafarers with Designated Security Duties (STSDSD)',
        ],
        ['Institute-specific pre-joining modular course certificates'],
        ['Company sponsorship / nomination or Letter of Guarantee'],
    ),
    'bosun': _matrix_entry(
        'deck', 'II/5',
        [
            'Certificate of Proficiency (COP) — Able Seafarer Deck (STCW II/5)',
            'Rating Forming Part of a Navigational Watch (RFPNW)',
        ],
        ['ARPA (Automatic Radar Plotting Aid)'],
        ['STCW II/5 endorsement after the source-stated sea-service prerequisite'],
    ),
    'able_seaman': _matrix_entry(
        'deck', 'II/5',
        [
            'Certificate of Proficiency (COP) — Able Seafarer Deck (STCW II/5)',
            'Rating Forming Part of a Navigational Watch (RFPNW)',
        ],
        ['Proficiency in Survival Craft and Rescue Boats (PSCRB)'],
        ['STCW II/5 endorsement after the source-stated 18 months sailing'],
    ),
    'ordinary_seaman': _matrix_entry(
        'deck', 'II/4',
        ['Rating Forming Part of a Navigational Watch (RFPNW)'],
        [],
        ['STCW II/4 endorsement after the source-stated six months sailing'],
    ),
    'engine_cadet': _matrix_entry(
        'engine', None,
        [
            'Pre-Sea Training completion certificate (marine engineering course as applicable)',
            'STCW Basic Safety Training (BST)',
            'Proficiency in Survival Craft and Rescue Boats (PSCRB)',
            'Security Training for Seafarers with Designated Security Duties (STSDSD)',
        ],
        ['Institute-specific pre-joining modular course certificates'],
        ['Company sponsorship / nomination or Letter of Guarantee'],
    ),
    'chief_engineer': _matrix_entry(
        'engine', 'III/2',
        [
            'Certificate of Competency (COC) — Chief Engineer (STCW III/2)',
            'Engine Room Resource Management (ERM)',
            'Advanced Fire Fighting (AFF)',
            'Medical First Aid (MFA)',
            'Ship Security Officer (SSO) or Security Awareness',
        ],
        ['High Voltage (HV) training', 'OEM-specific engine / machinery courses'],
        ['Oil / Chemical / Gas Tanker Familiarization and Dangerous Cargo Endorsement (DCE)',
         'High Voltage training where serving on HV-equipped vessels'],
    ),
    'second_engineer': _matrix_entry(
        'engine', 'III/2',
        [
            'Certificate of Competency (COC) — Engineer (STCW III/2)',
            'Engine Room Resource Management (ERM)',
            'Advanced Fire Fighting (AFF)',
            'Medical First Aid (MFA)',
            'Ship Security Officer (SSO) or Security Awareness',
        ],
        ['High Voltage (HV) training', 'OEM-specific engine / machinery courses'],
        ['Oil / Chemical / Gas Tanker Familiarization and Dangerous Cargo Endorsement (DCE)',
         'High Voltage training where serving on HV-equipped vessels'],
    ),
    'staff_engineer': _matrix_entry(
        'engine', 'III/2',
        [
            'Certificate of Competency (COC) — Engineer (STCW III/2)',
            'Engine Room Resource Management (ERM)',
            'Advanced Fire Fighting (AFF)',
            'Medical First Aid (MFA)',
            'Ship Security Officer (SSO) or Security Awareness',
        ],
        ['High Voltage (HV) training', 'OEM-specific engine / machinery courses'],
        ['Oil / Chemical / Gas Tanker Familiarization and Dangerous Cargo Endorsement (DCE)'],
    ),
    'first_engineer': _matrix_entry(
        'engine', 'III/2',
        [
            'Certificate of Competency (COC) — Engineer (STCW III/2)',
            'Engine Room Resource Management (ERM)',
            'Advanced Fire Fighting (AFF)',
            'Medical First Aid (MFA)',
            'Ship Security Officer (SSO) or Security Awareness',
        ],
        ['High Voltage (HV) training', 'OEM-specific engine / machinery courses'],
        ['Oil / Chemical / Gas Tanker Familiarization and Dangerous Cargo Endorsement (DCE)'],
    ),
    'third_engineer': _matrix_entry(
        'engine', 'III/1',
        [
            'Certificate of Competency (COC) — Engineer (STCW III/1)',
            'Advanced Fire Fighting (AFF)',
            'Medical First Aid (MFA)',
            'Ship Security Officer (SSO) or Security Awareness',
        ],
        ['Engine Room Resource Management (ERM)', 'High Voltage (HV) training'],
        ['Oil / Chemical / Gas Tanker Familiarization and Dangerous Cargo Endorsement (DCE)'],
    ),
    'fourth_engineer': _matrix_entry(
        'engine', 'III/1',
        [
            'Certificate of Competency (COC) — Engineer (STCW III/1)',
            'Advanced Fire Fighting (AFF)',
            'Medical First Aid (MFA)',
            'Ship Security Officer (SSO) or Security Awareness',
        ],
        ['Engine Room Resource Management (ERM)', 'High Voltage (HV) training'],
        ['Oil / Chemical / Gas Tanker Familiarization and Dangerous Cargo Endorsement (DCE)'],
    ),
    'hotel_engineer': _matrix_entry(
        'engine', 'III/2',
        [
            'Certificate of Competency (COC) — Engineer (STCW III/2)',
            'Engine Room Resource Management (ERM)',
            'Advanced Fire Fighting (AFF)',
            'Medical First Aid (MFA)',
            'Ship Security Officer (SSO) or Security Awareness',
        ],
        ['High Voltage (HV) training', 'Refrigeration / HVAC specialist certificate'],
        ['Oil / Chemical / Gas Tanker Familiarization and Dangerous Cargo Endorsement (DCE)'],
    ),
    'engine_rating': _matrix_entry(
        'engine', 'III/4',
        ['Rating Forming Part of an Engine Room Watch (RFPEW)'],
        [],
        ['Certificate of Proficiency as applicable to the rating'],
    ),
    'engine_mechanic': _matrix_entry(
        'engine', 'III/5',
        ['Certificate of Proficiency — Engine Rating (STCW III/5)'],
        ['Fitter certificate'],
        [],
    ),
    'chief_fireman': _matrix_entry(
        'engine', None,
        [],
        ['Advanced Fire Fighting (AFF)', 'Flash Over Practical Training',
         'Medical First Aid (MFA)'],
        ['Ship Security Officer (SSO) or Security Awareness'],
    ),
    'fireman': _matrix_entry(
        'engine', None,
        [],
        ['Flash Over Practical Training'],
        ['Advanced Fire Fighting (AFF)'],
    ),
    'welder': _matrix_entry(
        'engine', None,
        ['Welding certificate'],
        [],
        [],
    ),
    'security_officer': _matrix_entry(
        'deck', None,
        [],
        ['Ship Security Officer (SSO)'],
        ['Security Training for Seafarers with Designated Security Duties (STSDSD)'],
    ),
    'sanitation_officer': _matrix_entry(
        'deck', None,
        [],
        ['Advanced Fire Fighting (AFF)'],
        ['Passenger Ship Safety / Crowd and Crisis Management / Passenger Familiarization'],
    ),
    'eto': _matrix_entry(
        'eto', 'III/6',
        [
            'Certificate of Competency (COC) — Electro-Technical Officer (STCW III/6)',
            'Proficiency in Survival Craft and Rescue Boats (PSCRB)',
        ],
        [
            'Advanced Fire Fighting (AFF)',
            'Medical First Aid (MFA)',
            'High Voltage (HV) Safety and Switching',
        ],
        ['Marine electrical / engineering diploma where applicable'],
    ),
    'chief_eto': _matrix_entry(
        'eto', 'III/6',
        [
            'Certificate of Competency (COC) — Electro-Technical Officer (STCW III/6)',
            'Proficiency in Survival Craft and Rescue Boats (PSCRB)',
        ],
        [
            'Advanced Fire Fighting (AFF)',
            'Medical First Aid (MFA)',
            'High Voltage (HV) Safety and Switching',
        ],
        ['Marine electrical / engineering diploma where applicable'],
    ),
    'first_eto': _matrix_entry(
        'eto', 'III/6',
        [
            'Certificate of Competency (COC) — Electro-Technical Officer (STCW III/6)',
            'Proficiency in Survival Craft and Rescue Boats (PSCRB)',
        ],
        [
            'Advanced Fire Fighting (AFF)',
            'Medical First Aid (MFA)',
            'High Voltage (HV) Safety and Switching',
        ],
        ['Marine electrical / engineering diploma where applicable'],
    ),
    'second_eto': _matrix_entry(
        'eto', 'III/6',
        [
           'Certificate of Competency (COC) — Electro-Technical Officer (STCW III/6)',
           'Proficiency in Survival Craft and Rescue Boats (PSCRB)',
        ],
        [
           'Advanced Fire Fighting (AFF)',
           'Medical First Aid (MFA)',
           'High Voltage (HV) Safety and Switching',
        ],
        ['Marine electrical / engineering diploma where applicable'],
    ),
    'third_eto': _matrix_entry(
        'eto', 'III/6',
        [
           'Certificate of Competency (COC) — Electro-Technical Officer (STCW III/6)',
           'Proficiency in Survival Craft and Rescue Boats (PSCRB)',
        ],
        [
           'Advanced Fire Fighting (AFF)',
           'Medical First Aid (MFA)',
           'High Voltage (HV) Safety and Switching',
        ],
        ['Marine electrical / engineering diploma where applicable'],
    ),
    'electrician': _matrix_entry(
        # The workbook says III/7 is an optional/additional qualification
        # for electricians, not a mandatory rank code.
        'electrical', None,
        ['Proficiency in Survival Craft and Rescue Boats (PSCRB)'],
        ['Advanced Fire Fighting (AFF)', 'Medical First Aid (MFA)',
         'High Voltage (HV) Safety and Switching'],
        ['STCW III/7 certificate (additional/optional per MYC spreadsheet)'],
    ),
    'electrical_cadet': _matrix_entry(
        # The source lists ETO cadets separately and describes III/7 as
        # optional for additional electricians; it does not assign III/7
        # to an electrical cadet.
        'electrical', None,
        [
            'Pre-Sea Training completion certificate (electrical course as applicable)',
            'STCW Basic Safety Training (BST)',
            'Proficiency in Survival Craft and Rescue Boats (PSCRB)',
            'Security Training for Seafarers with Designated Security Duties (STSDSD)',
        ],
        ['Institute-specific pre-joining modular course certificates'],
        ['Company sponsorship / nomination or Letter of Guarantee'],
    ),
}


_RANK_ALIASES = {
    'master': 'master',
    'master mariner': 'master',
    'captain': 'master',
    'chief officer': 'chief_officer',
    'chief mate': 'chief_officer',
    'staff captain': 'chief_officer',
    'safety officer': 'chief_officer',
    'first officer': 'chief_officer',
    'second officer': 'second_officer',
    '2nd officer': 'second_officer',
    'environment officer': 'second_officer',
    'third officer': 'third_officer',
    '3rd officer': 'third_officer',
    'junior officer': 'junior_officer',
    'deck cadet': 'deck_cadet',
    'nautical cadet': 'deck_cadet',
    'bosun': 'bosun',
    'boatswain': 'bosun',
    'able seaman': 'able_seaman',
    'ab seaman': 'able_seaman',
    'ordinary seaman': 'ordinary_seaman',
    'os': 'ordinary_seaman',
    'chief engineer': 'chief_engineer',
    'chief eng': 'chief_engineer',
    'second engineer': 'second_engineer',
    '2nd engineer': 'second_engineer',
    'first assistant engineer': 'second_engineer',
    'first engineer': 'first_engineer',
    '1st engineer': 'first_engineer',
    'staff engineer': 'staff_engineer',
    'third engineer': 'third_engineer',
    '3rd engineer': 'third_engineer',
    'fourth engineer': 'fourth_engineer',
    '4th engineer': 'fourth_engineer',
    'hotel engineer': 'hotel_engineer',
    'engine cadet': 'engine_cadet',
    'engineering cadet': 'engine_cadet',
    'motorman': 'engine_rating',
    'oiler': 'engine_rating',
    'wiper': 'engine_rating',
    'engine rating': 'engine_rating',
    'engine mechanic': 'engine_mechanic',
    'fitter': 'engine_mechanic',
    'chief fireman': 'chief_fireman',
    'fireman': 'fireman',
    'welder': 'welder',
    'security officer': 'security_officer',
    'sanitation officer': 'sanitation_officer',
    'eto': 'eto',
    'electro technical officer': 'eto',
    'chief eto': 'chief_eto',
    'first eto': 'first_eto',
    'second eto': 'second_eto',
    'third eto': 'third_eto',
    'electrician': 'electrician',
    'electrical cadet': 'electrical_cadet',
    'electro technical cadet': 'electrical_cadet',
}

_CERTIFICATE_ALIASES = {
    'Passport': ('passport',),
    'CDC (Continuous Discharge Certificate)': ('cdc', 'discharge book'),
    'COP / COC': ('certificate of proficiency', 'certificate of competency', 'cop', 'coc'),
    'INDoS Number': ('indos',),
    "Seafarer's Identity Document (SID)": ('seafarer identity document', 'sid'),
    'Medical Fitness Certificate': (
        'eng1', 'eng 1', 'medical fitness', 'seafarer medical', 'medical certificate',
    ),
    'STCW Basic Safety Training (BST)': (
        'basic safety training', 'stcw basic safety', 'stcw 95', 'bst',
    ),
    'Proficiency in Survival Craft and Rescue Boats (PSCRB)': (
        'pscrb', 'proficiency in survival craft', 'survival craft and rescue boats',
    ),
    'Security Training for Seafarers with Designated Security Duties (STSDSD)': (
        'stsdsd', 'designated security duties',
    ),
    'Yellow Fever vaccination certificate': ('yellow fever',),
    'COVID-19 vaccination evidence': ('covid vaccination', 'covid-19 vaccination'),
    'Police Clearance Certificate': ('police clearance', 'police clearance certificate'),
    'Experience letters / appraisals / certificates of discharge': (
        'experience letter', 'appraisal', 'certificate of discharge',
    ),
    'ECDIS (generic and type-specific)': ('ecdis',),
    'GMDSS (GOC or ROC)': ('gmdss', 'goc', 'roc'),
    'Bridge Resource Management / Bridge Team Management (BRM / BTM)': (
        'bridge resource management', 'bridge team management', 'brm', 'btm',
    ),
    'Medical Care': ('medical care',),
    'ARPA (Automatic Radar Plotting Aid)': ('arpa', 'automatic radar plotting aid'),
    'Oil / Chemical / Gas Tanker Familiarization and Dangerous Cargo Endorsement (DCE)': (
        'oil tanker', 'chemical tanker', 'gas tanker', 'tanker familiarization', 'dce',
    ),
    'Passenger Ship Safety / Crowd and Crisis Management / Passenger Familiarization': (
        'passenger ship safety', 'crowd management', 'crisis management',
        'passenger familiarization', 'stcw v/2',
    ),
    'Pre-Sea Training completion certificate (nautical course as applicable)': (
        'pre-sea training', 'nautical science', 'diploma in nautical science',
    ),
    'Pre-Sea Training completion certificate (marine engineering course as applicable)': (
        'pre-sea training', 'marine engineering', 'graduate marine engineering',
    ),
    'Pre-Sea Training completion certificate (electrical course as applicable)': (
        'pre-sea training', 'electrical engineering', 'marine electronics',
    ),
    'Institute-specific pre-joining modular course certificates': (
        'pre-joining modular course', 'modular course certificate',
    ),
    'Company sponsorship / nomination or Letter of Guarantee': (
        'company sponsorship', 'letter of guarantee',
    ),
    'Certificate of Proficiency (COP) — Able Seafarer Deck (STCW II/5)': (
        'able seafarer deck', 'cop', 'stcw ii/5',
    ),
    'Rating Forming Part of a Navigational Watch (RFPNW)': ('rfpnw', 'navigational watch'),
    'Rating Forming Part of an Engine Room Watch (RFPEW)': ('rfpew', 'engine room watch'),
    'Certificate of Competency (COC) — Chief Engineer (STCW III/2)': (
        'chief engineer coc', 'coc stcw iii/2', 'certificate of competency stcw iii/2',
    ),
    'Certificate of Competency (COC) — Engineer (STCW III/2)': (
        'engineer coc stcw iii/2', 'coc stcw iii/2',
    ),
    'Certificate of Competency (COC) — Engineer (STCW III/1)': (
        'engineer coc stcw iii/1', 'coc stcw iii/1',
    ),
    'Certificate of Competency (COC) — Electro-Technical Officer (STCW III/6)': (
        'electro technical officer', 'eto coc', 'coc stcw iii/6', 'stcw iii/6',
    ),
    'Certificate of Proficiency — Engine Rating (STCW III/5)': ('stcw iii/5', 'engine rating'),
    'Engine Room Resource Management (ERM)': ('erm', 'engine room resource management'),
    'Advanced Fire Fighting (AFF)': ('advanced fire fighting', 'advanced firefighting', 'aff'),
    'Flash Over Practical Training': ('flash over', 'flashover practical training'),
    'Medical First Aid (MFA)': ('medical first aid', 'mfa'),
    'Fitter certificate': ('fitter certificate', 'fitter trade certificate'),
    'Welding certificate': ('welding certificate', 'welder certificate'),
    'Ship Security Officer (SSO) or Security Awareness': (
        'ship security officer', 'sso', 'security awareness',
    ),
    'High Voltage (HV) training': ('high voltage', 'hv training', 'hv safety'),
    'High Voltage (HV) Safety and Switching': ('high voltage', 'hv safety', 'hv switching'),
    'OEM-specific engine / machinery courses': ('man b&w', 'wartsila', 'wärtsilä', 'abb'),
}


def _aliases_for_cert(name: str) -> tuple[str, ...]:
    explicit = _CERTIFICATE_ALIASES.get(name)
    if explicit:
        return explicit
    without_codes = re.sub(r'\([^)]*\)', '', name)
    parts = [part.strip() for part in re.split(r'\s*/\s*', without_codes) if part.strip()]
    aliases = [without_codes.strip(), *parts]
    aliases.extend(
        token.strip()
        for group in re.findall(r'\(([^)]*)\)', name)
        for token in re.split(r'\s*(?:/|,|\bor\b)\s*', group, flags=re.IGNORECASE)
        if token.strip() and len(token.strip()) <= 12
    )
    return tuple(dict.fromkeys(alias.casefold() for alias in aliases if alias))


def normalize_cert_rank(rank: str) -> str:
    value = re.sub(r'[^a-z0-9]+', ' ', (rank or '').casefold()).strip()
    value = re.sub(r'\s+', ' ', value)
    if value in _RANK_ALIASES:
        return _RANK_ALIASES[value]
    return value.replace(' ', '_') if value.replace(' ', '_') in CERT_MATRIX else ''


def cert_requirements_for_rank(rank: str) -> dict:
    key = normalize_cert_rank(rank)
    return CERT_MATRIX.get(key, {})


def _all_matrix_certificates() -> dict[str, set[str]]:
    departments: dict[str, set[str]] = {}
    for entry in CERT_MATRIX.values():
        department = entry['department']
        for field in ('mandatory_certs', 'preferred_certs', 'conditional_certs'):
            for certificate in entry[field]:
                departments.setdefault(certificate, set()).add(department)
    return departments


_CERTIFICATE_DEPARTMENTS = _all_matrix_certificates()


def _certificate_is_present(name: str, certs_found: list[str], resume_text: str) -> bool:
    normalized_name = re.sub(r'[^a-z0-9]+', ' ', name.casefold()).strip()
    found = {
        re.sub(r'[^a-z0-9]+', ' ', str(value).casefold()).strip()
        for value in (certs_found or [])
    }
    if normalized_name in found:
        return True

    text = (resume_text or '').casefold()
    if 'stcw ii/' in name.casefold() or 'stcw iii/' in name.casefold():
        code = re.search(r'STCW\s+(II|III)/(\d+)', name, re.IGNORECASE)
        if not code or not re.search(rf'\b{code.group(1)}/{code.group(2)}\b', resume_text or '', re.IGNORECASE):
            return False
        if 'certificate of competency' in name.casefold() or '(coc)' in name.casefold():
            return bool(re.search(r'\b(?:coc|certificate of competency)\b', text))
        return True

    aliases = _aliases_for_cert(name)
    return any(re.search(rf'(?<!\w){re.escape(alias)}(?!\w)', text) for alias in aliases)


def _expiry_state(name: str, resume_text: str, today: date) -> CertEvidence | None:
    text = resume_text or ''
    lines = text.splitlines()
    aliases = _aliases_for_cert(name)
    for line in lines:
        line_lower = line.casefold()
        if not any(re.search(rf'(?<!\w){re.escape(alias)}(?!\w)', line_lower) for alias in aliases):
            continue
        if re.search(r'\b(expired|lapsed)\b', line_lower):
            return CertEvidence.EXPIRED
        if not re.search(r'\b(expir(?:y|es|ed)?|valid\s+(?:until|till|through))\b', line_lower):
            continue
        for match in re.finditer(r'\b(\d{1,2})[/-](\d{4})\b|\b(20\d{2})-(\d{2})-(\d{2})\b', line):
            try:
                if match.group(1):
                    year = int(match.group(2))
                    month = int(match.group(1))
                    expiry = date(year, month, calendar.monthrange(year, month)[1])
                else:
                    expiry = date(int(match.group(3)), int(match.group(4)), int(match.group(5)))
            except ValueError:
                continue
            if expiry < today:
                return CertEvidence.EXPIRED
    return None


def _explicitly_not_held(name: str, resume_text: str) -> bool:
    aliases = _aliases_for_cert(name)
    for line in (resume_text or '').splitlines():
        line_lower = line.casefold()
        if any(re.search(rf'(?<!\w){re.escape(alias)}(?!\w)', line_lower) for alias in aliases):
            if re.search(r'\b(?:not held|do not have|does not have|not available|never obtained)\b', line_lower):
                return True
    return False


def check_cert_evidence(
    rank: str,
    certs_found: list[str],
    resume_text: str,
    *,
    today: date | None = None,
) -> dict[str, CertEvidence]:
    """Return evidence states without inferring possession from silence."""
    today = today or datetime.utcnow().date()
    entry = cert_requirements_for_rank(rank)
    department = entry.get('department')
    applicable = set(CERT_UNIVERSAL_REQUIREMENTS)
    if entry:
        for field in ('mandatory_certs', 'preferred_certs', 'conditional_certs'):
            applicable.update(entry[field])

    result: dict[str, CertEvidence] = {}
    catalog = set(CERT_UNIVERSAL_REQUIREMENTS) | set(_CERTIFICATE_DEPARTMENTS)
    for name in sorted(catalog):
        departments = _CERTIFICATE_DEPARTMENTS.get(name, set())
        if department and departments and department not in departments:
            result[name] = CertEvidence.NOT_APPLICABLE
            continue
        if not entry and name not in CERT_UNIVERSAL_REQUIREMENTS:
            result[name] = CertEvidence.NOT_EVIDENCED
            continue
        if name not in applicable and name not in CERT_UNIVERSAL_REQUIREMENTS:
            result[name] = CertEvidence.NOT_EVIDENCED
            continue

        expiry = _expiry_state(name, resume_text, today)
        if expiry:
            result[name] = expiry
        elif _explicitly_not_held(name, resume_text):
            result[name] = CertEvidence.CONFIRMED_MISSING
        elif (
            name == 'Security Training for Seafarers with Designated Security Duties (STSDSD)'
            and any(
                alternative in resume_text.casefold()
                for alternative in ('ship security officer', 'sso', 'security awareness')
            )
        ):
            result[name] = CertEvidence.EVIDENCED
        elif _certificate_is_present(name, certs_found, resume_text):
            result[name] = CertEvidence.EVIDENCED
        else:
            result[name] = CertEvidence.NOT_EVIDENCED
    return result
