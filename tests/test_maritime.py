# tests/test_maritime.py
# Minimal tests for pipeline/maritime.py — cert/sea-service scoring across
# several ranks, including the two edge cases called out explicitly: a
# rank not present in CERT_REQUIREMENTS, and zero detected sea months.

from pipeline import maritime as m


# ── Pure lookup/regex functions ─────────────────────────────────────────

def test_cert_present_matches_alias():
    assert m.cert_present('STCW', 'holds valid stcw certificate') is True
    assert m.cert_present('GMDSS', 'no relevant certs here') is False


def test_detect_vessel_types_finds_known_types():
    assert m.detect_vessel_types('worked on chemical tanker and bulk carrier') == [
        'chemical tanker', 'bulk carrier',
    ]


def test_detect_vessel_types_empty_when_none_present():
    assert m.detect_vessel_types('no vessel keywords in this text') == []


def test_maritime_bonus_score_zero_for_unknown_rank_no_data():
    assert m.maritime_bonus_score('unknown', 0, [], [], []) == 0


# ── estimate_sea_service — PDF-first, text fallback (Task 1 fix) ───────

def test_estimate_sea_service_falls_back_to_text_when_no_pdf():
    text = 'Second Officer — ABC Shipping | 01/2020 - 06/2022'
    assert m.estimate_sea_service(pdf_path=None, text=text) > 0


def test_estimate_sea_service_zero_for_empty_input():
    assert m.estimate_sea_service(pdf_path=None, text='') == 0
    assert m.estimate_sea_service(pdf_path=None, text=None) == 0


# ── maritime_score() across multiple ranks ──────────────────────────────

def test_maritime_score_chief_officer_full_compliance():
    raw_text = (
        'Jane Smith\nChief Officer\n'
        'Holds Certificate of Competency (CoC), STCW, GMDSS, ENG1 medical certificate.\n'
        'Experience on chemical tanker vessels.\n'
    )
    parsed = {'sections': {'experience': 'Chief Officer — ABC Shipping | 01/2013 - 01/2020'}}
    result = m.maritime_score(raw_text, parsed, pdf_path=None)

    assert result['rank_detected'] == 'chief officer'
    assert result['missing_certs'] == []
    assert result['cert_coverage'] == '4/4'
    assert result['sea_months'] >= 72          # well above the 24mo minimum -> top bonus tier
    assert result['vessel_types'] == ['chemical tanker']
    assert result['maritime_grade'] == 'STRONG'
    assert result['maritime_score'] >= 80


def test_maritime_score_able_seaman_partial_compliance():
    raw_text = 'Ravi Kumar\nAble Seaman\nHolds STCW Basic Safety Training certificate.\n'
    parsed = {'sections': {'experience': 'Able Seaman — XYZ Marine | 01/2024 - 07/2024'}}
    result = m.maritime_score(raw_text, parsed, pdf_path=None)

    assert result['rank_detected'] == 'ab seaman'
    assert result['missing_certs'] == ['ENG1']
    assert result['cert_coverage'] == '1/2'
    assert 0 < result['sea_months'] < 12       # below the 12mo minimum for this rank
    assert result['vessel_types'] == []
    assert any('No vessel types detected' in i for i in result['issues'])
    assert any('Sea service' in i for i in result['issues'])


def test_maritime_score_unknown_rank_and_zero_sea_months():
    """
    Edge case: rank not detected at all (so not in CERT_REQUIREMENTS —
    falls back to the ['STCW', 'ENG1'] / 6-month defaults), combined with
    zero detected sea months. Should degrade gracefully, not crash or
    silently pass.
    """
    raw_text = 'Experienced professional seeking new opportunities.'
    parsed = {'sections': {'experience': ''}}
    result = m.maritime_score(raw_text, parsed, pdf_path=None)

    assert result['rank_detected'] == 'unknown'
    assert result['sea_months'] == 0
    assert result['missing_certs'] == ['STCW', 'ENG1']    # fallback defaults, not a KeyError
    assert result['cert_coverage'] == '0/2'
    assert result['vessel_types'] == []
    assert result['maritime_score'] == 0
    assert result['maritime_grade'] == 'WEAK'
    assert any('Rank/position not clearly stated' in i for i in result['issues'])
    assert any('Sea service duration not detected' in i for i in result['issues'])


def test_maritime_score_known_rank_but_zero_sea_months():
    """
    Edge case: zero sea months specifically, isolated from the
    unknown-rank case above — rank IS known and certs ARE complete, but
    sea service still can't be zeroed out silently.
    """
    raw_text = (
        'Third Officer\nHolds Certificate of Competency (CoC), STCW, ENG1 medical.\n'
        'Experience on bulk carrier.\n'
    )
    parsed = {'sections': {'experience': ''}}
    result = m.maritime_score(raw_text, parsed, pdf_path=None)

    assert result['rank_detected'] == 'third officer'
    assert result['missing_certs'] == []
    assert result['sea_months'] == 0
    assert any('Sea service duration not detected' in i for i in result['issues'])
