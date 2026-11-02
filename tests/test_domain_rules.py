from datetime import date

from generic_ats.career_track import (
    company_ats_platform,
    score_company_keywords,
    score_rank_keywords,
)
from generic_ats.layer import score_generic
from services.ats_format_checker import check_ats_format_compliance
from services.two_layer import (
    _rewrite_evaluation,
    check_rank_eligibility,
    estimate_years_at_rank,
    score_layers,
)
from rules.cert_rules import (
    CERT_MATRIX,
    CertEvidence,
    check_cert_evidence,
    normalize_cert_rank,
)


def test_certificate_evidence_distinguishes_all_states_without_inference():
    evidence = check_cert_evidence(
        'third officer',
        ['Passport', 'GMDSS'],
        (
            'Passport issued 01/2020\n'
            'CDC not held\n'
            'ENG1 expired 12/2024\n'
            'GMDSS GOC\n'
        ),
        today=date(2025, 1, 1),
    )

    assert evidence['Passport'] is CertEvidence.EVIDENCED
    assert evidence['CDC (Continuous Discharge Certificate)'] is CertEvidence.CONFIRMED_MISSING
    assert evidence['Medical Fitness Certificate'] is CertEvidence.EXPIRED
    assert evidence['Proficiency in Survival Craft and Rescue Boats (PSCRB)'] is CertEvidence.NOT_EVIDENCED
    assert evidence['Engine Room Resource Management (ERM)'] is CertEvidence.NOT_APPLICABLE


def test_stcw_iii_7_remains_optional_per_the_myc_spreadsheet():
    assert CERT_MATRIX['electrician']['stcw_code'] is None
    assert CERT_MATRIX['electrician']['regulatory_reference'] is None
    assert any('III/7' in value and 'optional' in value
               for value in CERT_MATRIX['electrician']['conditional_certs'])
    assert CERT_MATRIX['electrical_cadet']['stcw_code'] is None


def test_workbook_trade_roles_have_advisory_certificate_entries():
    assert normalize_cert_rank('Chief Fireman') == 'chief_fireman'
    assert normalize_cert_rank('Welder') == 'welder'
    assert normalize_cert_rank('Safety Officer') == 'chief_officer'
    assert normalize_cert_rank('Environment Officer') == 'second_officer'
    assert normalize_cert_rank('Sanitation Officer') == 'sanitation_officer'
    assert 'Flash Over Practical Training' in CERT_MATRIX['chief_fireman']['preferred_certs']
    assert 'Welding certificate' in CERT_MATRIX['welder']['mandatory_certs']


def test_generic_mode_does_not_apply_maritime_certificate_guidance_or_change_score():
    extracted = {
        'raw_text': (
            'Professional Summary\nExperienced professional in project operations.\n'
            'Experience\nManaged a team and improved delivery processes.\n'
            'Skills\nProject planning, communication, analysis\n'
        ),
        'structural_issues': [],
    }
    parsed = {
        'rank': '',
        'contact': {'email': 'sample@example.test', 'phone': '', 'linkedin': ''},
        'skills': ['Project planning', 'communication', 'analysis'],
        'sections': {
            'summary': 'Experienced professional in project operations.',
            'experience': 'Managed a team and improved delivery processes.',
            'skills': 'Project planning, communication, analysis',
            'education': '',
        },
    }
    job_description = 'Professional with project operations, planning, and analysis experience.'
    baseline = score_generic(
        extracted,
        parsed,
        job_description=job_description,
        role='',
        ext='pdf',
    )
    layers = score_layers(
        extracted,
        parsed,
        job_description=job_description,
        career_track='generic',
    )

    assert layers['generic']['total_score'] == baseline['total_score']
    assert layers['cert_evidence'] == {}
    assert layers['cert_requirements'] == {}
    assert layers['overlay'] is False
    assert layers['vacancy_match']['provided'] is True
    assert layers['score_explanation']['composition']['parse_points']['weight'] == 0.4


def test_rank_template_is_not_misrepresented_as_a_specific_vacancy():
    extracted = {'raw_text': 'Third Officer\nExperience\nBridge watchkeeping.', 'structural_issues': []}
    parsed = {'rank': 'third officer', 'sections': {'experience': 'Bridge watchkeeping.'}, 'skills': []}
    result = score_layers(extracted, parsed, career_track='third_officer')
    assert result['job_description']
    assert result['vacancy_match']['provided'] is False
    assert result['vacancy_match']['vacancy_match_score'] is None


def test_company_keyword_signal_uses_source_terms_and_is_separate():
    result = score_company_keywords(
        'Carnival',
        'deck',
        'Served as OOW and supported the Safety drill assessor role.',
    )

    assert result['company'] == 'carnival'
    assert result['matches'] == ['OOW', 'Safety drill assessor role']
    assert result['score'] > 0
    assert 'MYC reference' in result['source']


def test_rank_keyword_signal_uses_the_myc_rank_reference():
    result = score_rank_keywords(
        'third officer',
        'Third Officer with bridge watchkeeping and GMDSS experience.',
    )

    assert result['rank'] == 'third_officer'
    assert 'Bridge Watchkeeping' in result['matches']
    assert 'GMDSS' in result['matches']
    assert result['source'].startswith('ATS SOFTWARES.docx')


def test_engineer_keyword_signal_includes_myc_system_and_maintenance_terms():
    result = score_rank_keywords(
        'chief engineer',
        (
            'Chief Engineer responsible for main diesel engine operations, auxiliary machinery, '
            'fuel oil system maintenance, turbocharger maintenance, oily water separator operation, '
            'engine room resource management, dry dock supervision, and class surveys.'
        ),
    )

    assert 'Main Diesel Engine' in result['matches']
    assert 'Fuel Oil System' in result['matches']
    assert 'Oily Water Separator' in result['matches']
    assert 'Engine Room Resource Management' in result['matches']
    assert 'Dry Dock Supervision' in result['matches']


def test_eto_keyword_signal_includes_myc_electrical_and_automation_terms():
    result = score_rank_keywords(
        'eto',
        (
            'ETO with 6.6kV systems and 11kV systems switching, insulation resistance testing, '
            'variable frequency drive maintenance, PLC troubleshooting and SCADA systems maintenance, '
            'shore power connection, shaft generator, Azipod and dynamic positioning systems.'
        ),
    )

    assert '6.6kV Systems' in result['matches']
    assert '11kV Systems' in result['matches']
    assert 'Insulation Resistance Testing' in result['matches']
    assert 'SCADA Systems' in result['matches']
    assert 'Shore Power Connection' in result['matches']


def test_format_checker_is_advisory_and_reports_platform_qualified_findings():
    result = check_ats_format_compliance(
        'Experience\nSkills\nProjects:\n' + ('x' * 90),
        'successfactors',
        structural_issues=['MULTI_COLUMN_LAYOUT_DETECTED'],
    )

    assert result['errors'][0]['code'] == 'MULTI_COLUMN_LAYOUT'
    assert result['warnings']
    assert 'employer parser behavior varies' in result['note']


def test_ats_software_reference_platform_mapping_is_preserved_as_an_assumption():
    expected_assumptions = {
        'Carnival': ('unknown', 'Workday / SuccessFactors ecosystem'),
        'Royal Caribbean': ('workday', 'Workday'),
        'Norwegian': ('unknown', 'Taleo / Workday integrations'),
        'MSC': ('unknown', 'SuccessFactors / Workday'),
        'Disney': ('taleo', 'Taleo / Disney enterprise ATS'),
        'Virgin Voyages': ('greenhouse', 'Greenhouse / modern ATS stack'),
        'Princess': ('unknown', 'Carnival ecosystem ATS'),
        'Holland America': ('unknown', 'Carnival ecosystem ATS'),
        'Costa': ('successfactors', 'SAP SuccessFactors'),
        'AIDA': ('unknown', 'SAP ecosystem'),
        'Celebrity': ('workday', 'Workday'),
    }

    for company, (platform, assumption) in expected_assumptions.items():
        result = company_ats_platform(company)
        assert result['platform'] == platform
        assert result['assumption'] == assumption
        assert 'operational assumption' in result['source']


def test_rank_eligibility_uses_exact_two_year_boundary_and_requires_dates():
    at_boundary = check_rank_eligibility('third officer', 2.0, 'third officer')
    below_boundary = check_rank_eligibility('second officer', 1.9, 'third officer')
    unknown_tenure = check_rank_eligibility('third officer', None, 'third officer')

    assert at_boundary['recommended_rank'] == 'third officer'
    assert at_boundary['eligible_for_target'] is True
    assert below_boundary['recommended_rank'] == 'third officer'
    assert below_boundary['eligible_for_target'] is True
    assert unknown_tenure['status'] == 'needs_review'
    assert unknown_tenure['eligible_for_target'] is None
    assert 'not a regulatory' not in unknown_tenure['basis'].casefold()
    assert 'internal placement policy' in unknown_tenure['policy_source']


def test_eto_policy_directs_all_eto_seniority_to_third_eto():
    result = check_rank_eligibility('chief ETO', 12.0, 'third ETO')

    assert result['recommended_rank'] == 'third ETO'
    assert result['eligible_for_target'] is True
    assert 'including senior ETOs' in result['basis']


def test_rank_tenure_counts_the_end_month_and_ignores_supervisor_rank_mentions():
    experience = (
        'Third Officer | Example Shipping | 01/2022 - 12/2023\n'
        'Worked under the Chief Engineer on maintenance planning.'
    )

    years = estimate_years_at_rank(
        experience,
        'third officer',
        today=date(2024, 1, 1),
    )
    assert years == 2.0


def test_rewrite_fact_check_normalizes_dates_and_flags_new_facts():
    original_text = 'Third Officer\nBOSIET\nExperience\n01/2022 - 12/2023\nBridge watchkeeping.'
    rewritten_text = (
        'Third Officer\nBOSIET\nHUET\nExperience\nJan 2022 - Dec 2023\n'
        'Bridge watchkeeping with 25% improved efficiency.'
    )
    original_parsed = {
        'rank': 'third officer',
        'sections': {'experience': 'Third Officer | 01/2022 - 12/2023\nBridge watchkeeping.'},
    }
    rewritten_parsed = {
        'rank': 'third officer',
        'sections': {
            'experience': (
                'Third Officer | Jan 2022 - Dec 2023\n'
                'Bridge watchkeeping with 25% improved efficiency.'
            ),
        },
    }

    evaluation = _rewrite_evaluation(
        60,
        75,
        original_text,
        rewritten_text,
        original_parsed,
        rewritten_parsed,
        {'sea_months': 24},
        {'sea_months': 24},
    )

    assert evaluation['verdict'] == 'factual_integrity_risk'
    assert 'HUET' in evaluation['certs_added']
    assert any('25 percent' in value for value in evaluation['facts_changed'])


def test_rewrite_fact_check_flags_omissions_without_calling_them_inventions():
    original_text = 'Third Officer\nBOSIET\nExperience\n01/2022 - 12/2023\nBridge watchkeeping.'
    rewritten_text = 'Third Officer\nExperience\nJan 2022 - Dec 2023\nBridge watchkeeping.'
    original_parsed = {
        'rank': 'third officer',
        'sections': {'experience': 'Third Officer | 01/2022 - 12/2023\nBridge watchkeeping.'},
    }
    rewritten_parsed = {
        'rank': 'third officer',
        'sections': {'experience': 'Third Officer | Jan 2022 - Dec 2023\nBridge watchkeeping.'},
    }

    evaluation = _rewrite_evaluation(
        60,
        60,
        original_text,
        rewritten_text,
        original_parsed,
        rewritten_parsed,
        {'sea_months': 24},
        {'sea_months': 24},
    )

    assert evaluation['certs_lost'] == ['BOSIET']
    assert evaluation['verdict'] == 'degraded'
