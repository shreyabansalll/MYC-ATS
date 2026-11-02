"""Compare current and optional matching signals on labeled synthetic fixtures.

Synthetic rankings validate plumbing only; they are not candidate-quality evidence.
This evaluation data is separate from training data and must never be used as labels.
"""

from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from generic_ats.layer import score_generic
from generic_ats.keywords import extract_job_keywords, keyword_coverage
from services.semantic_matcher import semantic_skill_match
from services.vacancy_match import match_vacancy


TOKEN_RE = re.compile(r"[a-z0-9+#./-]+", re.I)


def tokenize(text: str) -> list[str]:
    return [token.casefold() for token in TOKEN_RE.findall(text or "")]


def bm25_score(query: str, document: str, corpus: list[str], *, k1: float = 1.5, b: float = 0.75) -> float:
    """Small Okapi BM25 implementation for offline evaluation only."""
    docs = [tokenize(item) for item in corpus]
    query_terms = set(tokenize(query))
    doc = tokenize(document)
    if not docs or not doc or not query_terms:
        return 0.0
    average_length = sum(map(len, docs)) / len(docs) or 1.0
    score = 0.0
    for term in query_terms:
        df = sum(term in set(candidate) for candidate in docs)
        if not df or term not in doc:
            continue
        idf = math.log(1 + (len(docs) - df + 0.5) / (df + 0.5))
        frequency = doc.count(term)
        denominator = frequency + k1 * (1 - b + b * len(doc) / average_length)
        score += idf * frequency * (k1 + 1) / denominator
    return score


def ranking_metrics(ranked_ids: list[str], relevance: dict[str, int], k: int) -> dict:
    top = ranked_ids[:k]
    total_relevant = sum(value > 0 for value in relevance.values())
    relevant_found = sum(relevance.get(identifier, 0) > 0 for identifier in top)
    precision = relevant_found / max(k, 1)
    recall = relevant_found / max(total_relevant, 1)
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    dcg = sum((2 ** relevance.get(identifier, 0) - 1) / math.log2(index + 2)
              for index, identifier in enumerate(top))
    ideal = sorted(relevance.values(), reverse=True)[:k]
    idcg = sum((2 ** value - 1) / math.log2(index + 2) for index, value in enumerate(ideal))
    return {
        'precision_at_k': round(precision, 4),
        'recall_at_k': round(recall, 4),
        'f1_at_k': round(f1, 4),
        'ndcg_at_k': round(dcg / idcg, 4) if idcg else 0.0,
    }


def _tfidf(query: str, document: str) -> float:
    vectors = TfidfVectorizer(ngram_range=(1, 2), stop_words='english').fit_transform([query, document])
    return float(cosine_similarity(vectors[0:1], vectors[1:2])[0][0])


def _candidate_scores(case: dict) -> dict[str, dict[str, float | None]]:
    query = case['job_description']
    candidates = case['candidates']
    corpus = [candidate['text'] for candidate in candidates]
    job_keywords = extract_job_keywords(query)
    scores = {}
    for candidate in candidates:
        text = candidate['text']
        extracted = {'raw_text': text, 'structural_issues': []}
        parsed = {
            'rank': '',
            'skills': [line.strip() for line in text.split('.') if line.strip()],
            'sections': {'summary': '', 'experience': text, 'skills': text, 'education': ''},
            'contact': {'email': '', 'phone': ''},
        }
        generic = score_generic(extracted, parsed, job_description=query, ext='pdf')
        keyword_pct, _ = keyword_coverage(text, job_keywords)
        vacancy = match_vacancy(query, text, parsed)
        semantic_component = semantic_skill_match(
            (vacancy['requirements'].get('required') or []) + (vacancy['requirements'].get('preferred') or []),
            parsed['skills'],
            certificate_requirements=vacancy['requirements'].get('certificates') or [],
        )
        semantic_score = semantic_component.get('semantic_score')
        tfidf_score = _tfidf(query, text)
        bm25 = bm25_score(query, text, corpus)
        vacancy_score = vacancy.get('vacancy_match_score')
        components = [generic['total_score'], vacancy_score or 0.0, min(bm25 * 10.0, 100.0)]
        if semantic_score is not None:
            components.append(semantic_score * 100.0)
        scores[candidate['candidate_id']] = {
            'existing_ats': float(generic['total_score']),
            'vacancy_keyword': float(keyword_pct),
            'vacancy_match': float(vacancy_score) if vacancy_score is not None else None,
            'tfidf_cosine': tfidf_score,
            'bm25': bm25,
            'semantic_similarity': semantic_score,
            'hybrid_exploratory': sum(components) / len(components),
        }
    return scores


def run_benchmark(dataset_path: Path | None = None) -> dict:
    dataset_path = dataset_path or ROOT_DIR / 'evaluation' / 'synthetic_matching_cases.json'
    dataset = json.loads(dataset_path.read_text())
    methods = ('existing_ats', 'vacancy_keyword', 'vacancy_match', 'tfidf_cosine', 'bm25', 'semantic_similarity', 'hybrid_exploratory')
    aggregate = {method: {metric: [] for metric in ('precision_at_k', 'recall_at_k', 'f1_at_k', 'ndcg_at_k')}
                 for method in methods}
    rows = []
    for case in dataset['cases']:
        scores = _candidate_scores(case)
        relevance = {candidate['candidate_id']: candidate['relevance'] for candidate in case['candidates']}
        rankings = {}
        for method in methods:
            ranked = sorted(
                (candidate['candidate_id'] for candidate in case['candidates']),
                key=lambda candidate_id: scores[candidate_id][method] if scores[candidate_id][method] is not None else -1.0,
                reverse=True,
            )
            rankings[method] = ranked
            if method == 'semantic_similarity' and all(scores[candidate][method] is None for candidate in scores):
                continue
            metrics = ranking_metrics(ranked, relevance, k=2)
            for key, value in metrics.items():
                aggregate[method][key].append(value)
        rows.append({'vacancy_id': case['vacancy_id'], 'scores': scores, 'rankings': rankings})
    report = {
        'evaluation': 'synthetic matching regression only',
        'real_candidate_data_used': False,
        'training_data': False,
        'semantic_available': bool(aggregate['semantic_similarity']['ndcg_at_k']),
        'k': 2,
        'per_method_mean_metrics': {
            method: {metric: round(sum(values) / len(values), 4) if values else None
                     for metric, values in metrics.items()}
            for method, metrics in aggregate.items()
        },
        'cases': rows,
        'limitations': [
            'The fixture set is synthetic and too small to support accuracy or model-selection claims.',
            'Hybrid weights are exploratory equal weights and are not production configuration.',
            'BM25 scores are scaled only for the illustrative hybrid and not calibrated.',
            'Semantic score is omitted when its optional local model is unavailable.',
        ],
    }
    return report


if __name__ == '__main__':
    print(json.dumps(run_benchmark(), indent=2))
