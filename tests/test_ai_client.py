# tests/test_ai_client.py
# Regression tests for services/ai_client.py::enforce_minimum_quality() —
# the deterministic minimum-quality patch applied after every Groq rewrite.
# Not full ai_client.py coverage (that needs a live/mocked Groq client);
# this covers the pure post-processing function.

import importlib
import sys

import config
from services.ai_client import enforce_minimum_quality


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
