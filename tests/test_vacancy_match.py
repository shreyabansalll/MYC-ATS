from datetime import date

from services.certificate_validity import track_certificate_validity
from services.vacancy_match import extract_vacancy_requirements, match_vacancy


def test_vacancy_requirements_and_skill_gaps_include_evidence_without_claiming_absence():
    vacancy = (
        "Third Officer\nRequired: GMDSS and Bridge Watchkeeping\n"
        "Preferred: Dynamic Positioning\nMinimum 2 years experience"
    )
    result = match_vacancy(
        vacancy,
        "Third Officer\nExperience\nBridge watchkeeping on passenger vessels.",
        {"rank": "third officer", "skills": ["Bridge Watchkeeping"]},
    )

    assert result["provided"] is True
    assert result["mandatory_eligibility"] == "needs_review"
    assert "GMDSS" in result["missing_mandatory"]
    assert result["skill_gaps"]["demonstrated"]
    assert result["skill_gaps"]["not_found"]
    assert result["mandatory_requirements"]
    assert result["recommendation"] == "review_required"


def test_no_vacancy_does_not_turn_generic_job_template_into_vacancy_match():
    result = match_vacancy("", "Third Officer with GMDSS", {"rank": "third officer"})
    assert result["vacancy_match_score"] is None
    assert result["mandatory_eligibility"] == "not_assessed"


def test_vacancy_extraction_only_marks_explicitly_labeled_requirements_mandatory():
    result = extract_vacancy_requirements(
        "Marine Officer\nPreferred: GMDSS, Dynamic Positioning"
    )
    assert result["required"] == []
    assert "GMDSS" in result["preferred"]


def test_requirement_section_applies_to_following_bullets_and_checks_rank():
    result = match_vacancy(
        "Third Officer\nRequired Qualifications:\n- GMDSS\n- Bridge Watchkeeping",
        "Second Officer\nExperience\nBridge watchkeeping.",
        {"rank": "second officer", "skills": ["Bridge Watchkeeping"]},
    )
    assert result["mandatory_eligibility"] == "needs_review"
    assert any(item["type"] == "rank" and item["status"] == "not_found" for item in result["mandatory_requirements"])


def test_partial_skill_overlap_is_possible_and_never_a_confirmed_match():
    result = match_vacancy(
        "Required: Passenger Vessel Familiarization",
        "Skills\nPassenger familiarization",
        {"skills": ["Passenger familiarization"]},
    )
    item = result["skill_gaps"]["items"][0]
    assert item["status"] == "possible"
    assert item["confidence"] == "medium"
    assert result["mandatory_eligibility"] == "needs_review"


def test_certificate_dates_are_explicit_and_use_configurable_reference_date():
    result = track_certificate_validity(
        "Passport expires 03/2027\nENG1 issued 01/2025, valid until 12/2026",
        today=date(2026, 10, 1),
        expiring_soon_days=183,
    )
    by_name = {item["certificate"]: item for item in result}
    assert by_name["Passport"]["status"] == "EXPIRING_SOON"
    assert by_name["Passport"]["expiry_date"] == "2027-03-31"
    assert by_name["Medical Fitness Certificate"]["status"] == "EXPIRING_SOON"
    assert by_name["Medical Fitness Certificate"]["issue_date"] == "2025-01-01"


def test_certificate_with_no_expiry_is_unknown_not_assumed_valid():
    result = track_certificate_validity("Passport issued 01/2020", today=date(2026, 1, 1))
    passport = next(item for item in result if item["certificate"] == "Passport")
    assert passport["status"] == "EXPIRY_UNKNOWN"
    assert passport["expiry_date"] is None


def test_certificate_validity_handles_expired_ambiguous_and_needs_review_states():
    result = track_certificate_validity(
        "ENG1 expires 12/2020\n"
        "Passport expiry pending\n"
        "CDC issued 01/2020 expires date unclear",
        today=date(2025, 1, 1),
    )
    by_name = {item["certificate"]: item for item in result}
    assert by_name["Medical Fitness Certificate"]["status"] == "EXPIRED"
    assert by_name["Passport"]["status"] == "DATE_AMBIGUOUS"
    assert by_name["CDC (Continuous Discharge Certificate)"]["status"] == "REQUIRES_VERIFICATION"


def test_certificate_evidence_redacts_document_numbers():
    result = track_certificate_validity(
        "Passport No ABC12345 expires 03/2027",
        today=date(2026, 1, 1),
    )
    passport = next(item for item in result if item["certificate"] == "Passport")
    assert "ABC12345" not in passport["evidence"]
    assert "[redacted]" in passport["evidence"]


def test_explicitly_missing_mandatory_certificate_blocks_but_silence_does_not():
    vacancy = "Third Officer\nRequired: GMDSS"
    silent = match_vacancy(vacancy, "Third Officer\nExperience\nBridge watchkeeping", {"rank": "third officer"})
    stated_missing = match_vacancy(
        vacancy,
        "Third Officer\nCertificates\nGMDSS not held",
        {"rank": "third officer"},
    )
    assert silent["mandatory_eligibility"] == "needs_review"
    assert stated_missing["mandatory_eligibility"] == "not_eligible_confirmed_missing"
