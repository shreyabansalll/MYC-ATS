"""Optional local-only semantic signal for vacancy skills.

This component is deliberately independent from ATS points and eligibility.
It never establishes certificate equivalence and labels candidates for human
verification only. No model or raw resume text is downloaded or cached.
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Callable

from config import (
    SEMANTIC_MATCH_ENABLED,
    SEMANTIC_MATCH_THRESHOLD,
    SEMANTIC_MODEL_PATH,
)


class LocalSemanticMatcher:
    def __init__(
        self,
        *,
        enabled: bool = SEMANTIC_MATCH_ENABLED,
        model_path: str = SEMANTIC_MODEL_PATH,
        threshold: float = SEMANTIC_MATCH_THRESHOLD,
        model_loader: Callable | None = None,
        cache_size: int = 128,
    ):
        self.enabled = enabled
        self.model_path = model_path
        self.threshold = threshold
        self.model_loader = model_loader
        self.cache_size = max(1, cache_size)
        self._model = None
        self._status = None
        self._query_cache: OrderedDict[str, tuple[float, ...]] = OrderedDict()
        self._lock = threading.RLock()

    def _load_model(self) -> bool:
        with self._lock:
            if self._model is not None:
                return True
            if self._status:
                return False
            if not self.enabled:
                self._status = 'disabled'
                return False
            if not self.model_path:
                self._status = 'model_path_not_configured'
                return False
            if not Path(self.model_path).is_dir():
                self._status = 'local_model_not_found'
                return False
            try:
                if self.model_loader:
                    self._model = self.model_loader(self.model_path)
                else:
                    from sentence_transformers import SentenceTransformer
                    self._model = SentenceTransformer(
                        self.model_path,
                        device='cpu',
                        local_files_only=True,
                        trust_remote_code=False,
                    )
                self._status = 'ready'
                return True
            except ModuleNotFoundError:
                self._status = 'optional_dependency_missing'
            except Exception:
                self._status = 'local_model_load_failed'
            return False

    @staticmethod
    def _key(text: str) -> str:
        return hashlib.sha256(text.casefold().strip().encode('utf-8')).hexdigest()

    def _encode_queries(self, terms: list[str]):
        missing, keys = [], []
        with self._lock:
            for term in terms:
                key = self._key(term)
                keys.append(key)
                if key in self._query_cache:
                    self._query_cache.move_to_end(key)
                else:
                    missing.append((key, term))
            if missing:
                vectors = self._model.encode(
                    [term for _, term in missing],
                    device='cpu',
                    normalize_embeddings=True,
                    convert_to_numpy=True,
                    show_progress_bar=False,
                    batch_size=16,
                )
                for (key, _), vector in zip(missing, vectors):
                    self._query_cache[key] = tuple(float(value) for value in vector)
                    while len(self._query_cache) > self.cache_size:
                        self._query_cache.popitem(last=False)
            return [self._query_cache[key] for key in keys]

    def compare(
        self,
        requirements: list[str],
        candidate_skills: list[str],
        *,
        certificate_requirements: list[str] | None = None,
    ) -> dict:
        excluded_values = list(dict.fromkeys(
            item.strip() for item in (certificate_requirements or [])
            if isinstance(item, str) and item.strip()
        ))
        excluded = {item.casefold() for item in excluded_values}
        terms = list(dict.fromkeys(
            term.strip()[:160] for term in requirements
            if isinstance(term, str) and term.strip() and term.casefold() not in excluded
        ))[:64]
        skills = list(dict.fromkeys(
            skill.strip()[:160] for skill in candidate_skills
            if isinstance(skill, str) and skill.strip()
        ))[:128]
        safe_base = {
            'enabled': self.enabled,
            'status': 'not_assessed',
            'semantic_score': None,
            'matches': [],
            'excluded_certificate_requirements': excluded_values,
            'note': 'Independent similarity signal only; it does not alter ATS points, mandatory eligibility, or prove qualification equivalence.',
        }
        if not self._load_model():
            safe_base['status'] = self._status or 'unavailable'
            return safe_base
        if not terms or not skills:
            safe_base['status'] = 'no_comparable_skills'
            return safe_base

        try:
            query_vectors = self._encode_queries(terms)
            candidate_vectors = self._model.encode(
                skills,
                device='cpu',
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
                batch_size=16,
            )
            matches = []
            for term, query in zip(terms, query_vectors):
                scores = [
                    sum(a * float(b) for a, b in zip(query, vector))
                    for vector in candidate_vectors
                ]
                best_index = max(range(len(scores)), key=scores.__getitem__)
                best_score = scores[best_index]
                matches.append({
                    'requirement': term,
                    'candidate_skill': skills[best_index],
                    'similarity': round(float(best_score), 4),
                    'status': 'possible' if best_score >= self.threshold else 'below_threshold',
                    'requires_manual_verification': True,
                })
            safe_base.update({
                'status': 'ready',
                'semantic_score': round(sum(item['similarity'] for item in matches) / len(matches), 4),
                'matches': matches,
            })
            return safe_base
        except Exception:
            safe_base['status'] = 'inference_failed'
            return safe_base


_DEFAULT_MATCHER = LocalSemanticMatcher()


def semantic_skill_match(
    requirements: list[str],
    candidate_skills: list[str],
    *,
    certificate_requirements: list[str] | None = None,
) -> dict:
    return _DEFAULT_MATCHER.compare(
        requirements,
        candidate_skills,
        certificate_requirements=certificate_requirements,
    )
