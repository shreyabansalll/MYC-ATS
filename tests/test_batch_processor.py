# tests/test_batch_processor.py
# Regression test for pipeline/batch_processor.py's success/failure
# classification — real I/O (a temp resume list + a real empty folder),
# isolated to a tmp_path via monkeypatch.chdir so it can't clobber the
# real data/outputs/batch_results.json from an actual 269-resume run.

import csv
import json
import os

from pipeline.batch_processor import run_batch, process_single


def test_process_single_no_resume_found_sets_a_real_error(tmp_path):
    """
    process_single() on a folder with no PDF inside returns
    name='NO_RESUME_FOUND' with a real (non-empty) error string — the
    fact run_batch used to special-case only ('ERROR', 'UNREADABLE') and
    miss this exact value is the bug; this confirms the return shape the
    fix now correctly classifies as a failure.
    """
    empty_folder = tmp_path / 'Some Candidate'
    empty_folder.mkdir()

    result = process_single(str(empty_folder))

    assert result['name'] == 'NO_RESUME_FOUND'
    assert result.get('error'), 'Expected a real error string for a folder with no resume PDF'


def test_run_batch_excludes_no_resume_found_from_success_averages(tmp_path, monkeypatch):
    """
    Real bug fixed: a folder with no resume PDF used to silently count as
    a "success" (name='NO_RESUME_FOUND' wasn't in the ('ERROR',
    'UNREADABLE') tuple run_batch checked), polluting batch_results.csv's
    averages/grade distribution with a bogus 0-score row. Runs the real
    run_batch() against a tiny real list (one genuinely empty folder),
    isolated to tmp_path so it can't touch the real repo's
    data/outputs/batch_results.json from an actual validation-set run.
    """
    monkeypatch.chdir(tmp_path)

    empty_folder = tmp_path / 'No Resume Candidate'
    empty_folder.mkdir()

    list_path = tmp_path / 'resume_file_list.txt'
    list_path.write_text(str(empty_folder) + '\n')

    run_batch(list_path=str(list_path))

    json_path = tmp_path / 'data' / 'outputs' / 'batch_results.json'
    assert json_path.exists()
    all_results = json.loads(json_path.read_text())
    assert len(all_results) == 1
    assert all_results[0]['name'] == 'NO_RESUME_FOUND'

    csv_path = tmp_path / 'data' / 'outputs' / 'batch_results.csv'
    if csv_path.exists():
        with open(csv_path) as f:
            rows = list(csv.DictReader(f))
        assert not any(r.get('name') == 'NO_RESUME_FOUND' for r in rows), (
            'NO_RESUME_FOUND must never appear in the CSV of successful results'
        )
    # If no CSV was written at all, that's also correct here (run_batch
    # only writes one `if results:`, and results should be empty since
    # the single input resolves to a failure) — the assertion above
    # already confirms it landed in the failed/all-results JSON, not the
    # success path, which is what this test exists to catch.
