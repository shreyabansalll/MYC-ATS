# tests/test_ai_client.py
# Regression tests for services/ai_client.py::enforce_minimum_quality() —
# the deterministic minimum-quality patch applied after every Groq rewrite.
# Not full ai_client.py coverage (that needs a live/mocked Groq client);
# this covers the pure post-processing function.

import importlib
import json
import sys
import types

import config
from services.ai_client import enforce_minimum_quality, sanitise_section


def test_ai_client_socks5h_proxy_env_builds_a_real_client(monkeypatch):
    """
    Regression test for a flawed version of this test: config.GROQ_API_KEY
    is read from os.environ exactly once, at config's first import, and
    cached on the module object. services/ai_client.py does
    `from config import GROQ_API_KEY` — a one-time attribute copy, not a
    live env read. Mutating os.environ (via monkeypatch.setenv/delenv) has
    NO effect on that cached value, so a version of this test that deleted
    GROQ_API_KEY from the environment never actually changed
    config.GROQ_API_KEY — it was already falsy from config's first import
    in this test process, meaning _build_groq_client()'s first
    `if not GROQ_API_KEY: return None` fired unconditionally and the
    socks5h branch below it was structurally unreachable, regardless of
    what this test did. The fix: patch config.GROQ_API_KEY directly (the
    actual value ai_client.py's `from config import GROQ_API_KEY` re-reads
    on module re-import), and assert something that can actually fail —
    `client is not None` — not the previous `hasattr(module, 'client')`,
    which is trivially true whether client is a real object or None.
    """
    monkeypatch.setattr(config, 'GROQ_API_KEY', 'fake-test-key-for-proxy-test')
    monkeypatch.setenv('HTTP_PROXY', 'socks5h://localhost:54937')
    monkeypatch.setenv('HTTPS_PROXY', 'socks5h://localhost:54937')

    sys.modules.pop('services.ai_client', None)
    module = importlib.import_module('services.ai_client')

    assert module.client is not None, (
        'Expected the socks5h proxy branch to actually construct a Groq '
        'client with a present API key — if this fails, the proxy-safe '
        'construction path is not actually working in this environment.'
    )


def test_ai_client_build_groq_client_logs_error_on_construction_failure(monkeypatch, caplog):
    """
    _build_groq_client() previously swallowed the real exception with a
    bare `except Exception: return None`, so a genuine construction
    failure (bad proxy config, missing SDK dependency, etc.) was
    invisible until it surfaced later as a generic 'GROQ client
    unavailable' RuntimeError with no root cause. Confirms the failure is
    now logged with the actual exception before returning None.
    """
    import groq as groq_module

    def _raise(*args, **kwargs):
        raise RuntimeError('boom: simulated construction failure')

    monkeypatch.setattr(groq_module, 'Groq', _raise)
    monkeypatch.setattr(config, 'GROQ_API_KEY', 'fake-test-key-for-error-logging-test')
    monkeypatch.delenv('HTTP_PROXY', raising=False)
    monkeypatch.delenv('HTTPS_PROXY', raising=False)
    monkeypatch.delenv('ALL_PROXY', raising=False)

    sys.modules.pop('services.ai_client', None)
    with caplog.at_level('ERROR'):
        module = importlib.import_module('services.ai_client')

    assert module.client is None, 'Construction failure must still degrade gracefully to None'
    assert any('Failed to construct Groq client' in r.message for r in caplog.records), (
        f'Expected the construction failure to be logged, got: {[r.message for r in caplog.records]}'
    )
    assert any('boom: simulated construction failure' in r.message for r in caplog.records), (
        'Expected the actual exception message to appear in the log, not just a generic notice'
    )


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


def test_rewrite_resume_retries_then_reports_incomplete_json_distinctly(monkeypatch):
    """
    Regression test: a Groq response cut off mid-generation (hitting
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

    class _FakeMessage:
        def __init__(self, content):
            self.content = content

    class _FakeChoice:
        def __init__(self, content):
            self.message = _FakeMessage(content)

    class _FakeResponse:
        def __init__(self, content):
            self.choices = [_FakeChoice(content)]

    class _FakeCompletions:
        def create(self, **kwargs):
            call_count['n'] += 1
            return _FakeResponse(incomplete_json)

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        chat = _FakeChat()

    monkeypatch.setattr(ai_client_module, 'client', _FakeClient())

    result = ai_client_module.rewrite_resume(
        parsed={'sections': {}, 'contact': {}, 'skills': [], 'raw_text': '', 'name': 'Test'},
        issues=[],
        rank='third officer',
    )
    assert result.get('error') == 'INCOMPLETE_RESPONSE', f'Expected INCOMPLETE_RESPONSE, got: {result}'
    assert call_count['n'] == 3, f'Expected 3 retry attempts, got {call_count["n"]}'


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

    class _FakeMessage:
        def __init__(self, content):
            self.content = content

    class _FakeChoice:
        def __init__(self, content):
            self.message = _FakeMessage(content)

    class _FakeResponse:
        def __init__(self, content):
            self.choices = [_FakeChoice(content)]

    class _FakeCompletions:
        def create(self, **kwargs):
            return _FakeResponse(complete_json)

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        chat = _FakeChat()

    monkeypatch.setattr(ai_client_module, 'client', _FakeClient())

    result = ai_client_module.rewrite_resume(
        parsed={'sections': {}, 'contact': {}, 'skills': [], 'raw_text': '', 'name': 'Test'},
        issues=[],
        rank='third officer',
    )
    assert 'error' not in result, f'Expected success, got error: {result}'
    assert result['summary'] == three_sentence_summary
