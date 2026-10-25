# pipeline/full_batch_processor.py
# Stage 1-6 batch validator — unlike pipeline/batch_processor.py (which
# only exercises Stages 1-4: extract/parse/score, fully deterministic,
# no Groq calls), this runs the REAL AI rewrite + DOCX generation for
# every candidate, then programmatically checks the actual generated
# output for the specific defect classes found by manual review this
# session (missing rank header, empty certifications/education/
# languages when source has real data, empty Sea Service Record when
# source has a real table, obviously truncated experience content).
#
# This does not replace manual review — it turns "someone happened to
# read this candidate's resume by hand" into a repeatable check that can
# run across a real, randomized sample instead of the same 2-3
# candidates every time.

import argparse
import json
import os
import random
import sys
import time

# Ensure repo root is on PYTHONPATH when script is run directly (same
# pattern as pipeline/batch_processor.py).
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from pipeline.extractor import extract_pdf
from pipeline.parser import parse_resume
from pipeline.scorer import score_resume
from pipeline.maritime import maritime_score
from models.candidate_profile import build_candidate_profile
from services.ai_client import rewrite_resume


def _has_real_gridded_experience(parsed: dict) -> bool:
    """True if the source experience section looks like a real gridded
    service-record table (pipe-delimited rows), not just prose."""
    text = parsed.get('sections', {}).get('experience', '')
    return any(line.count('|') >= 5 for line in text.split('\n'))


def check_candidate(path: str) -> dict:
    """
    Runs the real pipeline (Stages 1-5, no DOCX file written to disk —
    generate_docx isn't needed to check the REWRITE content itself,
    which is what every defect class found this session actually lives
    in) and returns a dict of pass/fail checks plus raw context for
    anything that failed, so a failure here is debuggable without
    re-running.
    """
    result = {'path': path, 'error': None, 'checks': {}}
    try:
        extracted = extract_pdf(path)
        if not extracted.get('raw_text', '').strip():
            result['error'] = f"extraction failed: {extracted.get('structural_issues')}"
            return result

        parsed = parse_resume(extracted['raw_text'])
        parsed['raw_text'] = extracted['raw_text']
        maritime_before = maritime_score(extracted['raw_text'], parsed, pdf_path=path)
        profile = build_candidate_profile(parsed, maritime_before)

        rewritten = rewrite_resume(
            parsed=parsed, issues=[], job_description='',
            rank=profile.rank_detected, license_rank=profile.license_rank,
            sea_months=profile.sea_months,
        )

        if 'error' in rewritten:
            # Real gap this session found and fixed: this used to mean
            # experience_bullets came back hard-empty regardless of
            # source content (api.py's fallback). This harness only
            # exercises rewrite_resume() directly (not api.py's
            # fallback wrapper), so a Groq failure here is reported as
            # its own check rather than silently masked by that fallback.
            result['error'] = f"rewrite failed: {rewritten.get('error')} — {rewritten.get('detail', '')[:200]}"
            return result

        checks = result['checks']

        checks['rank_present'] = bool(str(rewritten.get('rank', '')).strip())

        source_certs = parsed.get('sections', {}).get('certifications', '').strip()
        checks['certifications_ok'] = (not source_certs) or bool(rewritten.get('certifications'))

        source_edu = parsed.get('sections', {}).get('education', '').strip()
        checks['education_ok'] = (not source_edu) or bool(rewritten.get('education'))

        source_lang = parsed.get('sections', {}).get('languages', '').strip()
        checks['languages_ok'] = (not source_lang) or bool(rewritten.get('languages'))

        checks['sea_service_ok'] = (not _has_real_gridded_experience(parsed)) or bool(rewritten.get('experience_bullets'))

        total_bullet_words = sum(
            len(str(b).split())
            for job in rewritten.get('experience_bullets', []) if isinstance(job, dict)
            for b in job.get('bullets', [])
        )
        checks['experience_not_truncated'] = total_bullet_words >= 15 if rewritten.get('experience_bullets') else True

        result['passed'] = all(checks.values())
    except Exception as e:
        result['error'] = f'{type(e).__name__}: {e}'
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--sample-size', type=int, default=18)
    parser.add_argument('--seed', type=int, default=None)
    args = parser.parse_args()

    with open('data/resume_file_list.txt') as f:
        all_paths = [l.strip() for l in f if l.strip()]

    rng = random.Random(args.seed)
    sample = rng.sample(all_paths, min(args.sample_size, len(all_paths)))

    results = []
    for i, path in enumerate(sample, 1):
        name = path.split('/')[-1]
        print(f'[{i}/{len(sample)}] {name}...', flush=True)
        r = check_candidate(path)
        results.append(r)
        if r.get('error'):
            print(f'      ERROR: {r["error"]}')
        else:
            failed = [k for k, v in r['checks'].items() if not v]
            print(f'      {"PASS" if not failed else "FAIL: " + ", ".join(failed)}')
        # Real, confirmed account-level constraint (see config.py's
        # GROQ_MODEL comment): an 8000 TPM cap on this billing tier.
        # Spacing real, sequential calls out reduces (does not
        # eliminate) how often a candidate hits it and falls back.
        time.sleep(3)

    errored = [r for r in results if r.get('error')]
    checked = [r for r in results if not r.get('error')]
    passed  = [r for r in checked if r['passed']]

    print('\n' + '=' * 60)
    print(f'Total: {len(results)}  |  Errored: {len(errored)}  |  Checked: {len(checked)}  |  Passed all checks: {len(passed)}')
    if checked:
        for check_name in checked[0]['checks']:
            fail_count = sum(1 for r in checked if not r['checks'][check_name])
            print(f'  {check_name}: {len(checked) - fail_count}/{len(checked)} passed')

    with open('data/outputs/full_batch_results.json', 'w') as f:
        json.dump(results, f, indent=2)
    print('\nFull results: data/outputs/full_batch_results.json')


if __name__ == '__main__':
    main()
