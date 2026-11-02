import numpy as np

from services.semantic_matcher import LocalSemanticMatcher


class FakeEncoder:
    def __init__(self):
        self.calls = []

    def encode(self, texts, **_kwargs):
        self.calls.append(list(texts))
        vectors = {
            'bridge resource management': [1.0, 0.0],
            'bridge team management': [0.95, 0.05],
            'ecdIS operation': [0.0, 1.0],
            'gmdss': [0.0, 1.0],
        }
        return np.array([vectors[text] for text in texts], dtype=float)


def test_semantic_signal_is_separate_and_marks_candidates_for_review(tmp_path):
    model_dir = tmp_path / 'local-model'
    model_dir.mkdir()
    encoder = FakeEncoder()
    matcher = LocalSemanticMatcher(
        enabled=True,
        model_path=str(model_dir),
        model_loader=lambda _path: encoder,
        threshold=0.8,
    )

    result = matcher.compare(
        ['bridge resource management', 'GMDSS'],
        ['bridge team management', 'ecdIS operation'],
        certificate_requirements=['GMDSS'],
    )

    assert result['status'] == 'ready'
    assert result['matches'] == [{
        'requirement': 'bridge resource management',
        'candidate_skill': 'bridge team management',
        'similarity': 0.95,
        'status': 'possible',
        'requires_manual_verification': True,
    }]
    assert result['excluded_certificate_requirements'] == ['GMDSS']
    assert result['semantic_score'] == 0.95


def test_semantic_component_falls_back_when_disabled_or_model_missing(tmp_path):
    disabled = LocalSemanticMatcher(enabled=False).compare(['ECDIS'], ['ECDIS'])
    missing = LocalSemanticMatcher(enabled=True, model_path=str(tmp_path / 'missing')).compare(
        ['ECDIS'], ['ECDIS']
    )
    assert disabled['status'] == 'disabled'
    assert missing['status'] == 'local_model_not_found'
    assert disabled['semantic_score'] is None


def test_query_embedding_cache_is_bounded_and_does_not_store_raw_text_keys(tmp_path):
    model_dir = tmp_path / 'model'
    model_dir.mkdir()
    encoder = FakeEncoder()
    matcher = LocalSemanticMatcher(
        enabled=True,
        model_path=str(model_dir),
        model_loader=lambda _path: encoder,
        cache_size=2,
    )
    matcher.compare(['bridge resource management'], ['bridge team management'])
    matcher.compare(['bridge resource management'], ['bridge team management'])

    assert len(encoder.calls) == 3  # query once; candidate encoded for each request
    assert len(matcher._query_cache) <= 2
    assert 'bridge resource management' not in matcher._query_cache
