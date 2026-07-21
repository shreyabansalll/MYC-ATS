# tests/test_ai_client.py
# Regression tests for services/ai_client.py::enforce_minimum_quality() —
# the deterministic minimum-quality patch applied after every Groq rewrite.
# Not full ai_client.py coverage (that needs a live/mocked Groq client);
# this covers the pure post-processing function.

import importlib
import sys

from services.ai_client import enforce_minimum_quality


def test_ai_client_import_handles_proxy_env(monkeypatch):
    monkeypatch.setenv('HTTP_PROXY', 'socks5h://localhost:54937')
    monkeypatch.setenv('HTTPS_PROXY', 'socks5h://localhost:54937')
    monkeypatch.delenv('GROQ_API_KEY', raising=False)

    sys.modules.pop('services.ai_client', None)
    module = importlib.import_module('services.ai_client')

    assert hasattr(module, 'client')


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
