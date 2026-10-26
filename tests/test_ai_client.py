# tests/test_ai_client.py
# Regression tests for services/ai_client.py::enforce_minimum_quality() —
# the deterministic minimum-quality patch applied after every Claude rewrite.
# Not full ai_client.py coverage (that needs a live/mocked Anthropic client);
# this covers the pure post-processing function.

import importlib
import json
import sys
import types

import config
from services.ai_client import (
    enforce_minimum_quality, sanitise_section, enforce_cert_completeness,
    _normalize_rewrite_result, _stringify_cert, build_rewrite_prompt,
    _drop_fabricated_reference_numbers, restore_protected_signals,
    build_generic_rewrite_prompt,
)


def test_ai_client_socks5h_proxy_env_builds_a_real_client(monkeypatch):
    """
    Regression test for a flawed version of this test: config.ANTHROPIC_API_KEY
    is read from os.environ exactly once, at config's first import, and
    cached on the module object. services/ai_client.py does
    `from config import ANTHROPIC_API_KEY` — a one-time attribute copy, not a
    live env read. Mutating os.environ (via monkeypatch.setenv/delenv) has
    NO effect on that cached value, so a version of this test that deleted
    ANTHROPIC_API_KEY from the environment never actually changed
    config.ANTHROPIC_API_KEY — it was already falsy from config's first import
    in this test process, meaning _build_claude_client()'s first
    `if not ANTHROPIC_API_KEY: return None` fired unconditionally and the
    socks5h branch below it was structurally unreachable, regardless of
    what this test did. The fix: patch config.ANTHROPIC_API_KEY directly (the
    actual value ai_client.py's `from config import ANTHROPIC_API_KEY` re-reads
    on module re-import), and assert something that can actually fail —
    `client is not None` — not the previous `hasattr(module, 'client')`,
    which is trivially true whether client is a real object or None.
    """
    monkeypatch.setattr(config, 'ANTHROPIC_API_KEY', 'fake-test-key-for-proxy-test')
    monkeypatch.setenv('HTTP_PROXY', 'socks5h://localhost:54937')
    monkeypatch.setenv('HTTPS_PROXY', 'socks5h://localhost:54937')

    sys.modules.pop('services.ai_client', None)
    module = importlib.import_module('services.ai_client')

    assert module.client is not None, (
        'Expected the socks5h proxy branch to actually construct a Claude '
        'client with a present API key — if this fails, the proxy-safe '
        'construction path is not actually working in this environment.'
    )


def test_ai_client_build_claude_client_logs_error_on_construction_failure(monkeypatch, caplog):
    """
    _build_claude_client() previously swallowed the real exception with a
    bare `except Exception: return None`, so a genuine construction
    failure (bad proxy config, missing SDK dependency, etc.) was
    invisible until it surfaced later as a generic 'Claude client
    unavailable' RuntimeError with no root cause. Confirms the failure is
    now logged with the actual exception before returning None.
    """
    import anthropic as anthropic_module

    def _raise(*args, **kwargs):
        raise RuntimeError('boom: simulated construction failure')

    monkeypatch.setattr(anthropic_module, 'Anthropic', _raise)
    monkeypatch.setattr(config, 'ANTHROPIC_API_KEY', 'fake-test-key-for-error-logging-test')
    monkeypatch.delenv('HTTP_PROXY', raising=False)
    monkeypatch.delenv('HTTPS_PROXY', raising=False)
    monkeypatch.delenv('ALL_PROXY', raising=False)

    sys.modules.pop('services.ai_client', None)
    with caplog.at_level('ERROR'):
        module = importlib.import_module('services.ai_client')

    assert module.client is None, 'Construction failure must still degrade gracefully to None'
    assert any('Failed to construct Claude client' in r.message for r in caplog.records), (
        f'Expected the construction failure to be logged, got: {[r.message for r in caplog.records]}'
    )
    assert any('boom: simulated construction failure' in r.message for r in caplog.records), (
        'Expected the actual exception message to appear in the log, not just a generic notice'
    )


def test_claude_request_uses_configured_model(monkeypatch):
    from types import SimpleNamespace
    import services.ai_client as ai_client

    calls = {}

    class FakeMessages:
        def create(self, **kwargs):
            calls.update(kwargs)
            return SimpleNamespace(
                content=[SimpleNamespace(text="{}")],
                stop_reason="end_turn",
                usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            )

    monkeypatch.setattr(ai_client, "client", SimpleNamespace(messages=FakeMessages()))

    ai_client._call_claude_with_retry("test prompt")

    assert config.CLAUDE_MODEL == "claude-haiku-5-5"
    assert calls["model"] == config.CLAUDE_MODEL


def test_enforce_minimum_quality_deduplicates_skills_case_insensitively():
    """
    Regression test: raw (uncorrected) LLM output on a real generated
    resume repeated "Chemical Tanker", "Oil Tanker", and "Crisis
    Management" verbatim in the same Skills list. The padding logic that
    tops skills up to 15 only guarded against adding a *new* duplicate —
    it never removed ones the LLM had already produced on its own.
    """
    result = {
        'rank': 'chief officer',
        'skills': [
            'Chemical Tanker', 'Oil Tanker', 'Crisis Management',
            'chemical tanker', 'Oil Tanker', 'Cargo Operations',
        ],
    }
    fixed = enforce_minimum_quality(result, 'chief officer')
    lower_skills = [s.lower() for s in fixed['skills']]
    assert len(lower_skills) == len(set(lower_skills)), (
        f'Expected no case-insensitive duplicates, got {fixed["skills"]}'
    )
    # Original order/content preserved for the first occurrence of each skill.
    assert fixed['skills'][:4] == [
        'Chemical Tanker', 'Oil Tanker', 'Crisis Management', 'Cargo Operations',
    ]


def test_enforce_minimum_quality_still_pads_below_fifteen_after_dedup():
    """Padding to the 15-skill minimum still runs on the deduplicated list."""
    result = {
        'rank': 'oiler',
        'skills': ['Engine Room Watch', 'engine room watch', 'Bilge Operations'],
    }
    fixed = enforce_minimum_quality(result, 'oiler')
    assert len(fixed['skills']) > 3, 'Expected padding to top up the deduplicated list'
    lower_skills = [s.lower() for s in fixed['skills']]
    assert len(lower_skills) == len(set(lower_skills))


def test_enforce_minimum_quality_backfills_empty_role_with_real_company_and_dates():
    """
    Real bug fixed: the role-backfill for an entry with an empty 'role'
    string used to live inside the `if not exp or not has_real_exp:`
    branch's `else` clause -- which only runs when has_real_exp is
    FALSE, i.e. no entry anywhere has real company/dates. An entry with
    perfectly real company/dates but an empty role makes has_real_exp
    TRUE, so the whole outer `if` was skipped and the backfill never ran
    -- confirmed on real generated output where 'Electrical Cadet' was
    clean and parseable in the source, yet the role stayed blank in both
    Work Experience's header and the Sea Service Record row. This entry
    (real company/dates, empty role) is exactly that case.
    """
    result = {
        'rank': 'eto',
        'experience_bullets': [{
            'role': '',
            'company': 'Columbia Aurus Ship Management',
            'dates': '09/2024 - 05/2025',
            'bullets': ['Served aboard Front Feature (GRT 62795).'],
        }],
    }
    fixed = enforce_minimum_quality(result, 'eto')
    assert fixed['experience_bullets'][0]['role'].strip(), (
        f'Expected role to be backfilled, got: {fixed["experience_bullets"][0]}'
    )
    # Company/dates untouched -- only the empty field was backfilled.
    assert fixed['experience_bullets'][0]['company'] == 'Columbia Aurus Ship Management'
    assert fixed['experience_bullets'][0]['dates'] == '09/2024 - 05/2025'


def test_enforce_minimum_quality_backfills_role_across_mixed_entries():
    """Only the entry with an actually-empty role gets backfilled; a real role is left alone."""
    result = {
        'rank': 'third officer',
        'experience_bullets': [
            {'role': 'Third Officer', 'company': 'Real Co', 'dates': '01/2022 - 01/2023', 'bullets': []},
            {'role': '', 'company': 'Other Real Co', 'dates': '01/2020 - 12/2021', 'bullets': []},
        ],
    }
    fixed = enforce_minimum_quality(result, 'third officer')
    assert fixed['experience_bullets'][0]['role'] == 'Third Officer'
    assert fixed['experience_bullets'][1]['role'].strip()


def test_enforce_minimum_quality_does_not_crash_on_a_plain_string_experience_entry():
    """
    Real crash fixed, confirmed on a legacy model response
    output in a randomized 18-candidate sample batch run (3/18 hit this
    exact crash): the has_real_exp check called e.get('company', ...)
    on every item in experience_bullets with no isinstance(e, dict)
    guard -- unlike the similar check a few lines below it (added for
    the role-backfill fix above) -- so a response where an entry arrived
    as a plain string instead of a dict crashed with "'str' object has
    no attribute 'get'", losing the whole rewrite over one malformed
    entry rather than the whole thing still working, or that one entry
    being cleanly dropped.
    """
    result = {
        'rank': 'third officer',
        'experience_bullets': ['Worked as engineer on various vessels for 5 years.'],
    }
    fixed = enforce_minimum_quality(result, 'third officer')
    assert isinstance(fixed['experience_bullets'], list)


def test_normalize_rewrite_result_converts_a_plain_string_experience_entry_to_a_dict():
    """
    Defense-in-depth for the same real bug as the test above, one layer
    earlier: _normalize_rewrite_result must convert a bare string
    experience_bullets entry into a minimal real dict (empty role/
    company/dates, the string itself as the one bullet) so nothing
    downstream of it (enforce_minimum_quality, generate_docx) can crash
    on an unexpected type -- rather than relying on every single
    downstream consumer remembering to guard against it individually.
    """
    result = {
        'experience_bullets': ['Worked as engineer on various vessels for 5 years.'],
    }
    fixed = _normalize_rewrite_result(result, 'stop')
    entry = fixed['experience_bullets'][0]
    assert isinstance(entry, dict), f'Expected a dict, got: {entry!r}'
    assert entry['bullets'] == ['Worked as engineer on various vessels for 5 years.']


def test_normalize_experience_entry_folds_in_vessels_given_as_a_separate_list():
    """
    Real, confirmed content-loss bug on a legacy model response
    output: vessel names given as a separate list field (Rule 2, VESSEL
    DETAILS MANDATORY, requires them INSIDE the bullets) instead never
    made it into the rendered Work Experience section, since
    generate_docx() only ever reads 'bullets'. Matched by substring
    ('vessel' in the key name), not one exact key: confirmed unstable
    call to call for the SAME candidate across otherwise-identical real
    requests -- seen as both "vessels_served" and, separately, plain
    "vessels".
    """
    from services.ai_client import _normalize_experience_entry

    for key in ('vessels_served', 'vessels'):
        entry = {
            'role': 'Third Officer', 'company': 'Real Co', 'dates': '01/2022 - 01/2023',
            key: ['ASPEN SPIRIT', 'ATHEN SPIRIT'],
            'bullets': ['Conducted watchkeeping duties.'],
        }
        fixed = _normalize_experience_entry(entry)
        combined = ' '.join(fixed['bullets'])
        assert 'ASPEN SPIRIT' in combined and 'ATHEN SPIRIT' in combined, (
            f'Expected vessel names from {key!r} folded into bullets, got: {fixed["bullets"]}'
        )


def test_sanitise_section_truncates_at_line_boundary_not_mid_word():
    """
    Regression test: sanitise_section's truncation used to be a blind
    result[:max_chars] slice, which can cut a role entry off mid-word
    (or mid vessel-name/GRT) — confirmed as a real content-loss
    mechanism for multi-role service records exceeding the (previously
    too-tight) experience-section limit. The fix keeps only whole
    " | "-joined lines that fit the budget.
    """
    lines = [
        'Third Officer -- Eastern Pacific Shipping -- 07/2024 - 11/2025',
        'Executed cargo watches on M.T Zeal Start GRT 27987',
        'Junior Watchkeeping Officer -- Anglo Eastern -- 09/2023 - 03/2024',
        'Performed navigational watch on M.T SG Pegasus GRT 8195',
        'Deck Cadet -- Anglo Eastern -- 11/2019 - 03/2022',
        'Executed navigation tasks on M.V Ore Brasil GRT 199631',
    ]
    text = '\n'.join(lines)
    # Budget that fits some but not all lines whole.
    budget = len(lines[0]) + 3 + len(lines[1]) + 10
    result = sanitise_section(text, max_chars=budget)

    assert len(result) <= budget
    kept_lines = result.split(' | ')
    for line in kept_lines:
        assert line in lines, f'Truncation produced a fragment not matching a whole source line: {line!r}'
    # The first (most recent, per reverse-chronological convention) line
    # must survive intact -- not cut off mid-word.
    assert lines[0] in result


def test_sanitise_section_returns_untruncated_text_under_budget():
    text = 'Third Officer -- Eastern Pacific Shipping -- 07/2024 - 11/2025'
    result = sanitise_section(text, max_chars=1000)
    assert result == text


def test_build_rewrite_prompt_includes_verified_sea_months():
    """
    Real bug fixed, confirmed on real generated output: the prompt never
    gave the model the actual computed total sea-service duration, so it
    had to add up individual role date ranges itself to state one in the
    summary (Rule 1a) -- and got it badly wrong on a real candidate
    (wrote "24 months" for a verified total of 137 months, since
    overlapping contracts and gaps make a manual add-up unreliable). The
    verified figure must now be present in the prompt so the model can
    use it directly instead of calculating its own.
    """
    prompt = build_rewrite_prompt(
        parsed={'sections': {}, 'contact': {}, 'skills': [], 'raw_text': '', 'name': 'Test'},
        issues=[], job_description='', rank='chief officer', sea_months=137,
    )
    assert '137 months' in prompt
    assert 'verified' in prompt.lower()


def test_rewrite_prompts_include_protected_terms():
    signals = [
        {'text': 'FastAPI', 'kind': 'keyword', 'section': 'skills'},
        {'text': 'CDC MUM123456', 'kind': 'certificate', 'section': 'documents'},
    ]

    maritime_prompt = build_rewrite_prompt(
        _CRUISE_TEST_PARSED, [], '', rank='third officer',
        protected_signals=signals,
    )
    generic_prompt = build_generic_rewrite_prompt(
        _CRUISE_TEST_PARSED, [], '', protected_signals=signals,
    )

    for prompt in (maritime_prompt, generic_prompt):
        assert 'PROTECTED TERMS' in prompt
        assert 'do not rephrase, remove, or alter' in prompt
        assert '- FastAPI' in prompt
        assert '- CDC MUM123456' in prompt


def test_rewrite_resume_restores_a_model_dropped_protected_keyword(monkeypatch):
    import services.ai_client as ai_client_module

    response = json.dumps({
        'summary': 'First sentence here. Second sentence here. Third sentence here.',
        'experience_bullets': [],
        'skills': ['Python'],
        'certifications': [],
        'education': [],
        'documents': {},
        'languages': [],
    })
    monkeypatch.setattr(ai_client_module, 'client', _make_fake_client(response))

    result = ai_client_module.rewrite_resume(
        parsed={'sections': {}, 'contact': {}, 'skills': [], 'raw_text': '', 'name': 'Test'},
        issues=[],
        generic_mode=True,
        protected_signals=[
            {'text': 'FastAPI', 'kind': 'keyword', 'section': 'skills'},
        ],
    )

    assert 'error' not in result
    assert 'FastAPI' in result['skills']


def test_restore_protected_signals_puts_omitted_terms_in_their_source_sections():
    result = {
        'summary': 'Third Officer with marine experience. Maintained vessel systems. Served at sea.',
        'experience_bullets': [
            {'role': 'Third Officer', 'company': 'Example Co', 'dates': '2020 - 2022', 'bullets': ['Managed watchkeeping.']},
        ],
        'skills': ['Navigation'],
        'certifications': [],
        'education': [],
        'documents': {},
        'languages': [],
    }
    signals = [
        {'text': 'FastAPI', 'kind': 'keyword', 'section': 'skills'},
        {'text': 'MUM123456', 'kind': 'certificate_code', 'section': 'documents', 'restore_text': 'CDC No. MUM123456'},
        {'text': '27 months', 'kind': 'metric', 'section': 'experience', 'restore_text': 'Served 27 months aboard Example Vessel.'},
    ]

    fixed = restore_protected_signals(result, {}, signals)

    assert 'FastAPI' in fixed['skills']
    assert 'CDC No. MUM123456' in fixed['documents']['other']
    assert 'Served 27 months aboard Example Vessel.' in fixed['experience_bullets'][0]['bullets']


def test_restore_protected_section_heading_restores_source_content():
    fixed = restore_protected_signals(
        {'skills': []},
        {'sections': {'skills': 'Python\nFastAPI'}, 'skills': ['Python', 'FastAPI']},
        [{'text': 'Skills', 'kind': 'section_heading', 'section': 'skills'}],
    )

    assert fixed['skills'] == ['Python', 'FastAPI']
    assert 'Skills' not in fixed['skills']


def test_protected_signal_validation_preserves_exact_case():
    fixed = restore_protected_signals(
        {'skills': ['fastapi']},
        {},
        [{'text': 'FastAPI', 'kind': 'keyword', 'section': 'skills'}],
    )

    assert fixed['skills'] == ['fastapi', 'FastAPI']


_CRUISE_TEST_PARSED = {
    'sections': {'summary': 'Third officer with 5 years sea service.',
                 'experience': 'Sample experience text 01/2020 - 01/2021.'},
    'contact': {'email': 'test@test.com'},
    'skills': ['STCW', 'ECDIS'],
    'raw_text': 'raw text here',
    'name': 'Test Candidate',
}


def test_build_rewrite_prompt_without_cruise_line_is_byte_for_byte_unaffected():
    """
    Backward compatibility is the whole point of layering this on top of
    the existing rank-based system, not replacing it -- confirms the
    default (target_cruise_line omitted) and an explicit empty string
    both produce EXACTLY the same prompt as before this feature existed,
    not just "a similar one" or "one that still scores the same".
    """
    baseline = build_rewrite_prompt(
        _CRUISE_TEST_PARSED, [], '', rank='third officer', license_rank='', sea_months=24,
    )
    explicit_empty = build_rewrite_prompt(
        _CRUISE_TEST_PARSED, [], '', rank='third officer', license_rank='', sea_months=24,
        target_cruise_line='',
    )
    assert baseline == explicit_empty


def test_build_rewrite_prompt_with_known_cruise_line_includes_its_keywords():
    prompt = build_rewrite_prompt(
        _CRUISE_TEST_PARSED, [], '', rank='third officer', license_rank='', sea_months=24,
        target_cruise_line='Disney',
    )
    assert 'TARGET CRUISE LINE: Disney Cruise Line' in prompt
    assert 'Cast member mindset' in prompt
    # Grounding/no-fabrication rules must still be explicitly present --
    # this layer is a vocabulary preference, never a new source of facts.
    assert 'never a new source of facts' in prompt or 'never add a skill, cert, or experience claim' in prompt


def test_build_rewrite_prompt_with_a_different_recognized_line_gets_its_own_keywords():
    """Confirms this isn't hardcoded to one line -- a second real line resolves to its own distinct data."""
    prompt = build_rewrite_prompt(
        _CRUISE_TEST_PARSED, [], '', rank='third officer', license_rank='', sea_months=24,
        target_cruise_line='Royal Caribbean',
    )
    assert 'TARGET CRUISE LINE: Royal Caribbean Group' in prompt
    assert 'Oasis / Icon class' in prompt
    assert 'Cast member mindset' not in prompt  # Disney-specific, must not leak into RCL's prompt


def test_build_rewrite_prompt_with_unrecognized_cruise_line_falls_back_gracefully():
    """An invalid/misspelled cruise line must never error -- it degrades to plain rank-based behavior."""
    baseline = build_rewrite_prompt(
        _CRUISE_TEST_PARSED, [], '', rank='third officer', license_rank='', sea_months=24,
    )
    with_garbage = build_rewrite_prompt(
        _CRUISE_TEST_PARSED, [], '', rank='third officer', license_rank='', sea_months=24,
        target_cruise_line='NotARealCruiseLineXYZ',
    )
    assert baseline == with_garbage
    assert 'TARGET CRUISE LINE' not in with_garbage


def test_resolve_cruise_line_handles_common_spellings_and_abbreviations():
    from rules.cruise_line_keywords import resolve_cruise_line

    assert resolve_cruise_line('Disney') == 'disney'
    assert resolve_cruise_line('  disney cruise line  ') == 'disney'
    assert resolve_cruise_line('DCL') == 'disney'
    assert resolve_cruise_line('Royal Caribbean') == 'rcl'
    assert resolve_cruise_line('RCI') == 'rcl'
    assert resolve_cruise_line('') == ''
    assert resolve_cruise_line(None) == ''
    assert resolve_cruise_line('Some Random Airline') == ''


def test_rewrite_resume_retries_then_reports_incomplete_json_distinctly(monkeypatch):
    """
    Regression test: a Claude response cut off mid-generation (hitting
    max_tokens) can still parse as valid JSON if the truncation point
    happens to land on an earlier closing brace -- silently missing every
    field after that point (education, documents, languages, etc.) with
    no error at all. Confirms this is now caught by the required-keys
    check and reported as a distinct, diagnosable error after exhausting
    retries, rather than silently returned as if it were a complete,
    successful rewrite.
    """
    import services.ai_client as ai_client_module

    incomplete_json = json.dumps({'summary': 'A summary.', 'rank': 'third officer'})
    call_count = {'n': 0}

    monkeypatch.setattr(ai_client_module, 'client', _make_fake_client(incomplete_json, call_count))

    result = ai_client_module.rewrite_resume(
        parsed={'sections': {}, 'contact': {}, 'skills': [], 'raw_text': '', 'name': 'Test'},
        issues=[],
        rank='third officer',
    )
    assert result.get('error') == 'INCOMPLETE_RESPONSE', f'Expected INCOMPLETE_RESPONSE, got: {result}'
    assert call_count['n'] == 3, f'Expected 3 retry attempts, got {call_count["n"]}'


def _make_fake_client(response_json: str, call_counter: dict | None = None):
    class _FakeBlock:
        def __init__(self, text):
            self.text = text

    class _FakeMessage:
        def __init__(self, content):
            self.content = content

    class _FakeChoice:
        def __init__(self, content):
            self.message = _FakeMessage(content)
            self.finish_reason = 'stop'

    class _FakeUsage:
        prompt_tokens = 899
        completion_tokens = 962
        total_tokens = 1861
        input_tokens = 899
        output_tokens = 962

    class _FakeResponse:
        def __init__(self, content):
            self.choices = [_FakeChoice(content)]
            self.content = [_FakeBlock(content)]
            self.stop_reason = 'stop'
            self.usage = _FakeUsage()

    class _FakeCompletions:
        def create(self, **kwargs):
            if call_counter is not None:
                call_counter['n'] += 1
            return _FakeResponse(response_json)

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeMessages:
        def create(self, **kwargs):
            if call_counter is not None:
                call_counter['n'] += 1
            return _FakeResponse(response_json)

    class _FakeClient:
        chat = _FakeChat()
        messages = _FakeMessages()

    return _FakeClient()


def test_rewrite_resume_succeeds_when_json_has_all_required_keys(monkeypatch):
    """Confirms the required-keys check doesn't false-positive on a genuinely complete response."""
    import services.ai_client as ai_client_module

    # 3 sentences -- a single-sentence summary would trigger the
    # existing, unrelated enforce_minimum_quality() padding logic
    # (minimum 3 sentences), replacing it with a fallback template and
    # masking what this test is actually checking.
    three_sentence_summary = 'First sentence here. Second sentence here. Third sentence here.'
    complete_json = json.dumps({
        'summary': three_sentence_summary, 'rank': 'third officer',
        'experience_bullets': [], 'skills': ['STCW'], 'certifications': [],
        'education': [], 'documents': {}, 'languages': [],
    })

    monkeypatch.setattr(ai_client_module, 'client', _make_fake_client(complete_json))

    result = ai_client_module.rewrite_resume(
        parsed={'sections': {}, 'contact': {}, 'skills': [], 'raw_text': '', 'name': 'Test'},
        issues=[],
        rank='third officer',
    )
    assert 'error' not in result, f'Expected success, got error: {result}'
    assert result['summary'] == three_sentence_summary


def test_stringify_cert_handles_name_key_shape():
    """
    Real bug fixed: a certification dict shaped {"name": "..."} (no
    'type'/'number' key at all) fell through _stringify_cert's fixed
    key list to a bare str(cert) fallback -- a literal Python dict repr
    like "{'name': 'High Voltage Training Certificate'}" landing in the
    candidate's visible Certifications section, confirmed on real
    generated output.
    """
    result = {'certifications': [{'name': 'High Voltage Training Certificate'}]}
    fixed = _normalize_rewrite_result(result, 'stop')
    assert fixed['certifications'] == ['High Voltage Training Certificate']
    assert "{'name'" not in fixed['certifications'][0]


def test_stringify_cert_unknown_dict_shape_joins_values_not_repr():
    """An entirely unanticipated dict shape still produces readable text, never a raw repr."""
    assert _stringify_cert({'courseTitle': 'STCW Refresher', 'year': 2024}) == 'STCW Refresher | 2024'


def test_normalize_rewrite_result_stringifies_dict_valued_summary():
    """
    Real crash fixed, confirmed on a real live run: enforce_minimum_
    quality() calls split_into_sentences(result['summary']), which calls
    text.replace(...) with no type guard -- a 'summary' that arrived as
    a dict (schema drift, same category certifications/education already
    needed coercion for) crashed with "'dict' object has no attribute
    'replace'", losing an otherwise-successful rewrite entirely.
    """
    result = {'summary': {'text': 'A candidate summary.', 'tone': 'confident'}}
    fixed = _normalize_rewrite_result(result, 'stop')
    assert isinstance(fixed['summary'], str)
    assert 'A candidate summary.' in fixed['summary']
    # Must not crash downstream where a real caller uses it.
    enforce_minimum_quality({'summary': fixed['summary'], 'rank': 'third officer'}, 'third officer')


def test_normalize_rewrite_result_drops_bare_number_certification_entries():
    """
    Real bug fixed, confirmed on real output: the model extracted a
    Courses table's bare reference number ("10502363112502312") as a
    standalone certification entry, in ADDITION to using it correctly
    inside a full course-card entry elsewhere in the same list. A bare
    number with no letters carries no identifiable information on its
    own and must be dropped, not kept as a meaningless line item.
    """
    result = {
        'certifications': [
            'Certificate of Proficiency in Advanced Fire Fighting | Centre For Maritime Training | 10502363112502312',
            '10502363112502312',
            '1050233223250248',
            'STCW (STCW)',
        ],
    }
    fixed = _normalize_rewrite_result(result, 'stop')
    assert fixed['certifications'] == [
        'Certificate of Proficiency in Advanced Fire Fighting | Centre For Maritime Training | 10502363112502312',
        'STCW (STCW)',
    ]


def test_normalize_rewrite_result_drops_fabricated_acronym_number_pair():
    """
    Real bug fixed, confirmed on real generated output: given a source
    Courses table with a reference number sitting near (but not
    describing) generic maritime acronyms elsewhere in the source, the
    model fabricated an entry shaped exactly '(ECDIS) | 10502364112502510'
    -- the SAME reference number that legitimately belongs to a
    different, real course-card entry ("...Medical First Aid... |
    10502364112502510"). The bare acronym-in-parens-plus-number shape,
    when its number is confirmed to belong to a different real entry, is
    dropped as fabrication.
    """
    result = {
        'certifications': [
            'Certificate of Proficiency in Medical First Aid | Centre For Maritime | 10502364112502510',
            '(ECDIS) | 10502364112502510',
        ],
    }
    fixed = _normalize_rewrite_result(result, 'stop')
    assert fixed['certifications'] == [
        'Certificate of Proficiency in Medical First Aid | Centre For Maritime | 10502364112502510',
    ]


def test_normalize_rewrite_result_drops_fabrication_regardless_of_exact_wording():
    """
    Generalized on purpose, not matched to one fixed string: confirmed
    across multiple real live calls that the fabricated wording itself
    varies ('(ECDIS) | <number>' one call, '(ECDIS) Course | <number>'
    another, a real-sounding title paired with a DIFFERENT course's
    number a third time) -- this is genuinely nondeterministic LLM
    output, not one bug with one exact reproduction string. Whatever the
    wording, the tell is the same reference number appearing in two
    entries; the shorter of the two is dropped regardless of shape.
    """
    result = {
        'certifications': [
            'Certificate of Proficiency in Medical First Aid | Centre For Maritime | 10502364112502510',
            '(ECDIS) Course | 10502364112502510',
            'Oil and Chemical Tanker Cargo Operations | Centre for Maritime Training-Noida | 10502351112402515',
            'Medical First Aid | 10502351112402515',
        ],
    }
    fixed = _normalize_rewrite_result(result, 'stop')
    assert fixed['certifications'] == [
        'Certificate of Proficiency in Medical First Aid | Centre For Maritime | 10502364112502510',
        'Oil and Chemical Tanker Cargo Operations | Centre for Maritime Training-Noida | 10502351112402515',
    ]


def test_normalize_rewrite_result_keeps_bare_acronym_pair_when_number_unconfirmed():
    """
    Conservative by design: if the bare-shape entry's number doesn't
    appear anywhere else, it can't be confirmed as a mislabeled
    duplicate from this data alone -- kept rather than guessed at, since
    dropping real (if terse) content is the worse failure mode.
    """
    result = {'certifications': ['(GMDSS) | 19999999999999']}
    fixed = _normalize_rewrite_result(result, 'stop')
    assert fixed['certifications'] == ['(GMDSS) | 19999999999999']


def test_normalize_rewrite_result_recovers_from_real_partial_wrapper_drift():
    """
    Real bug fixed, confirmed on a real live model response (Jimmy
    Fernandes): only SOME fields (here, "skills") were nested under an
    invented "candidate" wrapper key, while "summary"/"experience"/
    "education"/"certifications" were already top-level siblings in the
    SAME response. The original unwrap logic only fired when the entire
    payload had exactly one top-level key -- this response has five, so
    it was never unwrapped, and "skills" (skills the wrapper) stayed
    hidden inside "candidate", permanently missing from the top level no
    matter how many retries ran. This also exercises the per-entry
    'summary' (a string) instead of 'bullets' (a list) drift confirmed
    in the same real response.
    """
    raw_shaped_response = {
        'candidate': {
            'name': 'JIMMY FERNANDES',
            'currentRank': 'Second Officer',
            'totalSeaService': 114,
            'skills': ['STCW', 'CoC', 'Crisis Management', 'COLREGS', 'MARPOL'],
        },
        'summary': 'Highly competent Second Officer with solid hands-on experience.',
        'experience': [
            {
                'rank': 'Second Officer',
                'company': 'Fleet Management Ltd',
                'vesselName': 'MT. MAERSK KALMAR',
                'GRT': 29999,
                'dates': '01/02/2020 - 31/07/2020',
                'summary': 'Responsible for navigation, bridge watchkeeping and cargo operations. '
                           'Ensured compliance with international maritime standards.',
            },
        ],
        'education': [{'institution': 'Maritime Academy', 'dates': '2010 - 2014', 'summary': 'OOW certification.'}],
        'certifications': [{'certificate': 'Officer Of The Watch (OOW)', 'issuingAuthority': 'Maritime Authority'}],
    }

    result = _normalize_rewrite_result(raw_shaped_response, 'stop')

    assert result.get('skills') == ['STCW', 'CoC', 'Crisis Management', 'COLREGS', 'MARPOL'], (
        f'Expected skills recovered from the partial "candidate" wrapper, got: {result.get("skills")}'
    )
    assert result.get('summary'), 'Expected the already-top-level summary to survive untouched'
    assert result['experience_bullets'][0]['role'] == 'Second Officer'
    assert result['experience_bullets'][0]['bullets'], (
        f'Expected the per-entry "summary" string to convert into a real bullets list, got: {result["experience_bullets"][0]}'
    )
    assert isinstance(result['experience_bullets'][0]['bullets'], list)
    assert 'certificate' not in ' '.join(str(c) for c in result['certifications'])


def test_rewrite_resume_recovers_from_real_wrapper_and_field_name_drift(monkeypatch):
    """
    Regression test for a real production failure (job debug log, a real
    candidate resume -- name/email/phone/document numbers below are
    scrubbed, but the JSON *shape* matches the mocked provider response).
    finish_reason was "stop" with completion_tokens (962) far under
    max_tokens (6000) -- confirmed via the provider's response fields, not a
    guess, that this was never truncation.

    The model nested the entire payload under an invented "candidate"
    wrapper key, renamed "experience_bullets" to "experience" (and inside
    each entry, "role"/"dates"/"bullets" to
    "rank"/"startDate"+"endDate"/"responsibilities"), renamed
    "technical_expertise" to camelCase "technicalExpertise", returned
    certifications as dicts instead of the prompt's flat strings, and
    omitted "documents"/"languages" entirely (folding passport/visa/CDC/
    INDOS numbers into the certifications dicts instead).

    Before the fix: REQUIRED_REWRITE_KEYS found nothing at the top level
    and reported every field as missing, even though a complete, good
    rewrite existed one level down. A naive "just unwrap the wrapper" fix
    would still have crashed inside enforce_minimum_quality's
    cert.replace() call on the dict-shaped certifications, and would have
    silently rendered an empty role/dates/bullets Work Experience section
    -- worse than today's honest failure-with-fallback, since maritime
    scoring reads vessel name/GRT out of that text. This test confirms
    the real fix recovers all of it, not just the wrapper.
    """
    import services.ai_client as ai_client_module

    real_shaped_response = json.dumps({
        'candidate': {
            'firstName': 'TEST',
            'lastName': 'CANDIDATE',
            'email': 'test.candidate@example.com',
            'phone': '+91 9000000000',
            'currentRank': 'Eto',
            'summary': (
                'A motivated Electrical Cadet with a solid foundation in '
                'electrical engineering, circuit design, and power '
                'distribution. Skilled in CAD software and engineering '
                'tools for design, testing, and maintenance. Experienced '
                'in high voltage systems and shipboard automation.'
            ),
            'experience': [
                {
                    'rank': 'Electrical Cadet',
                    'company': 'Example Ship Management',
                    'vesselName': 'MT Example Star',
                    'vesselGRT': 62795,
                    'startDate': '09/2024',
                    'endDate': '05/2025',
                    'responsibilities': [
                        'Assisted the Electrical Officer in the maintenance and '
                        'troubleshooting of shipboard electrical systems, including '
                        'power distribution, generators, and automation systems.',
                        'Conducted routine inspections and preventive maintenance of '
                        'switchboards, motors, and lighting systems.',
                    ],
                },
                {
                    'rank': 'Electrical Cadet',
                    'company': 'Example Ship Management',
                    'vesselName': 'MT Example Star',
                    'vesselGRT': 62795,
                    'startDate': '09/2024',
                    'endDate': '05/2025',
                    'responsibilities': [
                        'Gained hands-on experience with high-voltage systems, '
                        'emergency supply systems and ship communication networks.',
                    ],
                },
            ],
            'education': [
                {
                    'institution': 'Example Maritime University',
                    'degree': 'B.Tech in Electronics and Communication',
                    'graduationDate': '2021',
                },
            ],
            'skills': [
                'STCW', 'Medical First Aid', 'Crisis Management', 'High Voltage',
                'Chemical Tanker', 'Oil Tanker', 'Communication',
                'GMDSS Equipment Maintenance', 'High Voltage Systems', 'ECDIS',
                'AIS Maintenance', 'VDR Maintenance', 'Bridge Navigation Systems',
                'Automation Systems', 'PLC Troubleshooting',
            ],
            'technicalExpertise': [
                'Generators - Expertise in fuel-efficient engines; proficient in Yanmar.',
            ],
            'certifications': [
                {'type': 'PASSPORT', 'number': 'X0000000', 'expirationDate': '21/JAN/2030'},
                {'type': 'US VISA', 'number': 'X0000001', 'expirationDate': '16/JUN/2029'},
                {'type': 'CDC', 'number': 'MUM 000000', 'expirationDate': '16/APR/2034'},
                {'type': 'INDOS No.', 'number': '24AB0000'},
                {'type': 'High Voltage Training Certificate'},
            ],
            'references': 'Available on request',
        },
    })

    call_count = {'n': 0}
    monkeypatch.setattr(ai_client_module, 'client', _make_fake_client(real_shaped_response, call_count))

    result = ai_client_module.rewrite_resume(
        parsed={'sections': {}, 'contact': {}, 'skills': [], 'raw_text': '', 'name': 'Test Candidate'},
        issues=[],
        rank='eto',
    )

    assert 'error' not in result, f'Expected the unwrapped/normalized response to succeed, got: {result}'
    assert call_count['n'] == 1, (
        f'Expected the first Claude response to be recovered and accepted -- no retries needed. '
        f'Got {call_count["n"]} attempts.'
    )

    # Wrapper unwrapped, top-level key renamed back to schema.
    experience = result['experience_bullets']
    assert len(experience) == 2

    # Per-entry field-name drift recovered: rank -> role, responsibilities ->
    # bullets, startDate/endDate -> dates.
    first = experience[0]
    assert first['role'] == 'Electrical Cadet'
    assert first['dates'] == '09/2024 - 05/2025'
    assert len(first['bullets']) >= 2

    # Vessel name/GRT folded into the bullet text (Rule 2: VESSEL DETAILS
    # MANDATORY) -- it arrived as a separate structured field, not
    # mentioned in either responsibility sentence.
    assert any('MT Example Star' in b for b in first['bullets']), (
        f'Expected vessel name to be folded into a bullet, got: {first["bullets"]}'
    )

    # Certifications flattened to strings -- left as dicts, these would
    # have crashed enforce_minimum_quality's cert.replace() call.
    assert all(isinstance(c, str) for c in result['certifications']), (
        f'Expected all certifications to be strings, got: {result["certifications"]}'
    )

    # technicalExpertise (camelCase) recovered to the schema's real name.
    assert result.get('technical_expertise'), 'Expected technicalExpertise to be recovered as technical_expertise'

    # Document-like entries (passport/visa/CDC/INDOS) were mixed into
    # "certifications" in the raw response -- _reclassify_document_like_certs
    # must have moved them into "documents" rather than leaving them
    # cluttering the Certifications section next to actual courses/certs.
    certs_combined = ' '.join(result['certifications']).lower()
    assert 'passport' not in certs_combined and 'visa' not in certs_combined, (
        f'Expected passport/visa to be reclassified out of certifications, got: {result["certifications"]}'
    )
    assert result['documents'].get('passport'), 'Expected passport to be filed under documents'
    assert result['documents'].get('cdc'), 'Expected CDC to be filed under documents'
    assert result['documents'].get('indos'), 'Expected INDOS to be filed under documents'
    assert any('visa' in str(o).lower() for o in result['documents'].get('other', [])), (
        f'Expected US Visa (no dedicated slot) under documents["other"], got: {result["documents"]}'
    )

    # languages was never present at all in the real response -- defaulted
    # to its empty value since finish_reason == 'stop' confirms this
    # wasn't a mid-generation cutoff.
    assert result['languages'] == []


# ── Priority 0: cert/course completeness safety net ─────────────────────
# Regression tests for enforce_cert_completeness() using a real candidate's
# cert/document data (PII scrubbed to synthetic values, shape and count
# unchanged: 5 real documents -- passport/visa/CDC/INDOS/yellow fever -- and
# 5 real courses/certificates, matching the real source resume that
# motivated this safety net). This is a real, confirmed bug: generator.py's
# enforce_length_limits() used to hard-cap certifications at 8 lines,
# silently dropping 2 of these 10 real entries even though the AI rewrite
# itself had preserved all 10 -- the candidate's real generated DOCX was
# missing "Certificate of Proficiency in Advanced Fire Fighting" and
# "High Voltage Safety and Switch Gear Course" before this fix.

_SOURCE_SECTIONS = {
    'certifications': (
        'High Voltage Training Certificate\n'
        'Passenger Familiarization and Crowd & Crisis Management Certification\n'
        'All required Basic & Advanced STCW Courses\n'
        'Certificate of Proficiency in Advanced Fire Fighting\n'
        'High Voltage Safety and Switch Gear Course\n'
    ),
    'documents': (
        'Passport No. X0000000 Exp 21/01/2030\n'
        'US Visa No. X0000001 Exp 16/06/2029\n'
        'CDC No. MUM000000 Exp 16/04/2034\n'
        'INDOS No. 24AB0000\n'
        'Yellow Fever Certificate No. 000000\n'
    ),
    'license': '',
}
_SOURCE_PARSED = {'sections': _SOURCE_SECTIONS}


def test_enforce_cert_completeness_appends_dropped_items():
    """
    The safety net itself, not just the happy path: simulates an AI
    rewrite that dropped 4 of the 10 real cert/document items (the exact
    failure confirmed on real output) and confirms all four are restored,
    verbatim, without disturbing the items that were already present.

    US Visa and Yellow Fever are checked in 'documents' (under the
    'other' bucket), not 'certifications' -- a later fix made
    enforce_cert_completeness re-run _reclassify_document_like_certs
    after appending, so a restored document-shaped item (not just ones
    the model produced itself) gets correctly filed there instead of
    cluttering the rendered Certifications section. Confirmed this is a
    real improvement, not data loss: the item is still present, just in
    its correct location.
    """
    result = {
        'certifications': [
            'High Voltage Training Certificate',
            'Passenger Familiarization and Crowd & Crisis Management Certification',
            'All required Basic & Advanced STCW Courses',
            # 'Certificate of Proficiency in Advanced Fire Fighting' -- dropped
            # 'High Voltage Safety and Switch Gear Course' -- dropped
        ],
        'documents': {
            'passport': 'X0000000, exp 21/01/2030',
            'cdc':      'MUM000000, exp 16/04/2034',
            'indos':    '24AB0000',
            # US Visa and Yellow Fever have no dedicated documents field
            # and weren't restated anywhere else either -- also dropped.
        },
        'skills': ['STCW', 'High Voltage'],
    }

    fixed = enforce_cert_completeness(result, _SOURCE_PARSED)

    combined = ' '.join(fixed['certifications']).lower()
    assert 'certificate of proficiency in advanced fire fighting' in combined, (
        f'Expected the dropped fire-fighting cert to be restored, got: {fixed["certifications"]}'
    )
    assert 'high voltage safety and switch gear course' in combined, (
        f'Expected the dropped switch-gear course to be restored, got: {fixed["certifications"]}'
    )
    documents_other = ' '.join(fixed.get('documents', {}).get('other', [])).lower()
    assert 'us visa' in documents_other, (
        f'Expected the dropped US Visa document to be restored under documents["other"], got: {fixed.get("documents")}'
    )
    assert 'yellow fever' in documents_other, (
        f'Expected the dropped Yellow Fever document to be restored under documents["other"], got: {fixed.get("documents")}'
    )
    assert 'us visa' not in combined and 'yellow fever' not in combined, (
        f'Expected these to be reclassified OUT of certifications, not left there too: {fixed["certifications"]}'
    )

    # Items that were already present must not be duplicated or altered.
    assert fixed['certifications'][0] == 'High Voltage Training Certificate'
    assert fixed['certifications'][1] == 'Passenger Familiarization and Crowd & Crisis Management Certification'
    assert fixed['certifications'][2] == 'All required Basic & Advanced STCW Courses'


def test_enforce_cert_completeness_no_op_when_everything_already_covered():
    """Happy path: nothing appended, nothing duplicated, when the rewrite already retained every source item."""
    result = {
        'certifications': [
            'High Voltage Training Certificate',
            'Passenger Familiarization and Crowd & Crisis Management Certification',
            'All required Basic & Advanced STCW Courses (STCW)',
            'Certificate of Proficiency in Advanced Fire Fighting',
            'High Voltage Safety and Switch Gear Course',
        ],
        'documents': {
            'passport': 'X0000000',
            'cdc':      'MUM000000',
            'indos':    '24AB0000',
        },
        'skills': ['US Visa X0000001', 'Yellow Fever Certificate 000000'],
    }
    original_certs = list(result['certifications'])

    fixed = enforce_cert_completeness(result, _SOURCE_PARSED)

    assert fixed['certifications'] == original_certs, (
        f'Expected no changes when every source item is already covered, got: {fixed["certifications"]}'
    )


def test_enforce_cert_completeness_handles_no_source_certs_gracefully():
    """A candidate with no certifications/documents/license section at all is a no-op, not an error."""
    result = {'certifications': ['STCW'], 'documents': {}, 'skills': []}
    fixed = enforce_cert_completeness(result, {'sections': {}})
    assert fixed['certifications'] == ['STCW']


def test_enforce_education_completeness_restores_dropped_source_entries():
    """
    Real, confirmed gap: 'education' was previously NOT in
    _DEFAULTABLE_REWRITE_KEYS, so a response missing it entirely (a real,
    repeated behavior in legacy model responses -- confirmed
    finish_reason == 'stop', i.e. NOT truncated, on 3/3 real retry
    attempts for the same candidate) was treated as a truncation signal,
    exhausting every retry and losing the ENTIRE rewrite over one
    non-critical field. This is the restore-don't-invent safety net
    (mirroring enforce_cert_completeness) that makes 'education'
    defaultable safe: real source content is recovered rather than only
    silently defaulted to empty.
    """
    from services.ai_client import enforce_education_completeness

    parsed = {'sections': {'education': (
        'Bachelors of Technology in Marine Engineering\n'
        'Tolani Maritime Institute\n'
        '2021 Maharashtra, India\n'
    )}}
    result = {'education': []}
    fixed = enforce_education_completeness(result, parsed)
    combined = ' '.join(fixed['education']).lower()
    assert 'tolani maritime institute' in combined, (
        f'Expected the dropped education entry to be restored, got: {fixed["education"]}'
    )


def test_enforce_language_completeness_restores_dropped_source_entries_but_not_noise():
    """
    Real, confirmed gap, same class as education above: 'languages' was
    routinely omitted from real model output even when the
    source resume genuinely listed languages. Also confirms this
    function deliberately does NOT use _looks_like_real_source_line
    (unlike certifications/education): a bare one-word line like
    'English' is exactly what that filter would reject as noise for a
    certifications-shaped blob, but is completely legitimate here.
    """
    from services.ai_client import enforce_language_completeness

    parsed = {'sections': {'languages': 'English\nArabic\n'}}
    result = {'languages': []}
    fixed = enforce_language_completeness(result, parsed)
    assert 'English' in fixed['languages']
    assert 'Arabic' in fixed['languages']


def test_enforce_cert_completeness_never_appends_extraction_noise():
    """
    Real bug fixed, confirmed on a real candidate's actual parsed section
    text: enforce_cert_completeness used to line-split the raw
    certifications/documents/license section blob directly and treat
    every line as a candidate source item -- including structural
    extraction noise that had bled into that section (a stray header-row
    date of birth, isolated words swept in from the Summary section by a
    column-bleed artifact, and a table field glued to a reference number
    with no separator). All three concrete patterns, taken from the real
    extraction, must now be filtered out rather than appended verbatim
    into the candidate's visible Certifications section.
    """
    noisy_sections = {
        # ​: zero-width space, exactly as prefixed on the real
        # header-row date-of-birth this bug was confirmed against.
        'certifications': (
            'High Voltage Training Certificate\n'
            '​21/12/1999\n'            # bare header-row DOB
            'hands-\n'                      # isolated word swept in from Summary
            'industry\n'                    # isolated word swept in from Summary
            'Training10502363112502312\n'   # institute name glued to a reference number
        ),
        'documents': '',
        'license': '',
    }
    result = {'certifications': [], 'documents': {}, 'skills': []}
    fixed = enforce_cert_completeness(result, {'sections': noisy_sections})

    combined = ' '.join(fixed['certifications'])
    assert '21/12/1999' not in combined, f'Bare date leaked into output: {fixed["certifications"]}'
    assert 'hands-' not in fixed['certifications'], f'Stray word leaked into output: {fixed["certifications"]}'
    assert 'industry' not in fixed['certifications'], f'Stray word leaked into output: {fixed["certifications"]}'
    assert 'Training10502363112502312' not in combined, (
        f'Glued table artifact leaked into output: {fixed["certifications"]}'
    )
    # The one genuine line in that noisy blob must still survive.
    assert any('High Voltage Training Certificate' in c for c in fixed['certifications'])


def test_enforce_cert_completeness_merges_wrapped_cert_line_not_a_fragment():
    """
    Real bug fixed, confirmed on real output (Ankur Shrivastava): the
    source resume wraps one certificate's name across two consecutive
    lines -- pipeline/parser.py's own clean section split produces
    exactly 'Passenger Familiarization and Crowd &\nCrisis Management
    Certification.' as two lines, a plain PDF line-wrap, not column
    bleed. The old line-by-line split treated these as two independent
    source items; since "Crisis Management" happened to already appear
    in the rewrite's Skills list, the safety net considered the second
    half "covered" and appended only the orphaned first half alone --
    a visibly broken half-sentence in the candidate's Certifications
    section. The merged, complete phrase must be appended instead.
    """
    noisy_sections = {
        'certifications': (
            'High Voltage Training Certificate.\n'
            'Passenger Familiarization and Crowd &\n'
            'Crisis Management Certification.\n'
            'All required Basic STCW Courses.\n'
        ),
        'documents': '',
        'license': '',
    }
    result = {
        'certifications': [],
        'documents': {},
        # "Crisis Management" and "High Voltage" already present as
        # skills, exactly as in the real captured output -- this is what
        # made the safety net (wrongly) think the wrapped line's second
        # half was already covered.
        'skills': ['STCW', 'Crisis Management', 'High Voltage'],
    }
    fixed = enforce_cert_completeness(result, {'sections': noisy_sections})

    assert 'Passenger Familiarization and Crowd &' not in fixed['certifications'], (
        f'Expected no orphaned fragment, got: {fixed["certifications"]}'
    )
    assert any(
        'Passenger Familiarization and Crowd & Crisis Management Certification' in c
        for c in fixed['certifications']
    ), f'Expected the merged, complete cert name, got: {fixed["certifications"]}'


def test_normalize_rewrite_result_drops_certification_number_not_in_source():
    """
    Real bug fixed, confirmed on real output (Ankur Shrivastava): the AI
    rewrite's own certifications list contained two separate
    "Certificate of Competency (CoC)" entries with numbers "123456" and
    "789012" -- neither appears anywhere in the source resume text at
    all (confirmed by grepping the actual source PDF). Classic
    sequential placeholder-number fabrication, distinct from
    _drop_fabricated_acronym_number_pairs (which only catches the SAME
    number reused across entries -- here each entry has its own
    invented number, so that check never fires). The fabricated numbers
    must be stripped and the now-identical entries deduped.
    """
    raw_text = (
        'Ankur Shrivastava Second Engineer ... COC 95X - 9194 - 12/06/2026 '
        'MEO CLASS II Passport Y1683454 CDC CH51310 INDoS 04EL0592'
    )
    result = {
        'certifications': [
            'Certificate of Competency (CoC) | 123456',
            'Certificate of Competency (CoC) | 789012',
        ],
    }
    fixed = _normalize_rewrite_result(result, finish_reason='stop')
    fixed['certifications'] = _drop_fabricated_reference_numbers(fixed['certifications'], raw_text)

    assert '123456' not in ' '.join(fixed['certifications'])
    assert '789012' not in ' '.join(fixed['certifications'])
    assert fixed['certifications'] == ['Certificate of Competency (CoC)'], (
        f'Expected the fabricated numbers stripped and the now-duplicate entries deduped, got: {fixed["certifications"]}'
    )


def test_rewrite_resume_full_pipeline_restores_dropped_certs(monkeypatch):
    """
    Integration test: enforce_cert_completeness runs inside rewrite_resume()
    itself (after enforce_minimum_quality), using the same real 10-item
    cert/document source data, via a mocked Claude response that drops 2 of
    them -- confirms the safety net fires as part of the real call path,
    not just as a standalone unit.
    """
    import services.ai_client as ai_client_module

    three_sentence_summary = 'First sentence here. Second sentence here. Third sentence here.'
    incomplete_json = json.dumps({
        'summary': three_sentence_summary,
        'rank': 'eto',
        'experience_bullets': [{'role': 'ETO', 'company': 'Example Co', 'dates': '01/2020 - 01/2021', 'bullets': ['Did work.']}],
        'skills': ['STCW'] * 15,
        'certifications': [
            'High Voltage Training Certificate',
            'Passenger Familiarization and Crowd & Crisis Management Certification',
            'All required Basic & Advanced STCW Courses',
        ],
        'education': ['Some Degree — Some University — 2020'],
        'documents': {'passport': 'X0000000', 'cdc': 'MUM000000', 'indos': '24AB0000'},
        'languages': [],
    })

    monkeypatch.setattr(ai_client_module, 'client', _make_fake_client(incomplete_json))

    result = ai_client_module.rewrite_resume(
        parsed={**_SOURCE_PARSED, 'contact': {}, 'skills': [], 'raw_text': '', 'name': 'Test'},
        issues=[],
        rank='eto',
    )

    assert 'error' not in result, f'Expected success, got: {result}'
    combined = ' '.join(result['certifications']).lower()
    assert 'certificate of proficiency in advanced fire fighting' in combined
    assert 'high voltage safety and switch gear course' in combined
