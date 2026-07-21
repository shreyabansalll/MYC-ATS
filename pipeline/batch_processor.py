# pipeline/batch_processor.py
# Batch processes all resumes in resume_file_list.txt
# Outputs: data/outputs/batch_results.csv + batch_results.json
# Run: venv/bin/python pipeline/batch_processor.py

import os
import sys
import json
import csv
import time
import warnings

# Suppress harmless FontBBox warnings from pdfplumber on Canva PDFs
warnings.filterwarnings('ignore', message='.*FontBBox.*')

# Ensure repo root is on PYTHONPATH when script is run directly.
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from pipeline.extractor import extract_text
from pipeline.parser import parse_resume
from pipeline.scorer import score_resume
from pipeline.maritime_scorer import maritime_score


def process_single(file_path: str) -> dict:
    """Process one resume through full pipeline. Returns result dict."""
    try:
        # If path is a directory, find the best PDF inside it
        if os.path.isdir(file_path):
            
            # Nested function definition
            def select_best_resume(folder_path: str) -> str | None:
                """
                Given a candidate folder, select the most likely resume PDF.
                Excludes cover letters, picks largest file if multiple remain.
                """
                import glob
                EXCLUDE_KEYWORDS = ['cover', 'letter', 'cl_', '_cl.', 'coverl', 'coverletter']
                
                all_pdfs = glob.glob(os.path.join(folder_path, '*.pdf'))
                resumes = [p for p in all_pdfs
                        if not any(kw in os.path.basename(p).lower() for kw in EXCLUDE_KEYWORDS)]
                
                if not resumes:
                    return None
                if len(resumes) == 1:
                    return resumes[0]
                
                # Multiple PDFs — take the largest (resume is always bigger than cover letter)
                return max(resumes, key=os.path.getsize)

            file_path = select_best_resume(file_path)
            if not file_path:
                return {'file': file_path, 'name': 'NO_RESUME_FOUND',
                        'error': 'No valid resume PDF found in folder', 'ats_score': 0, 'maritime_score': 0}

        # 1. Dynamically get the extension (e.g. 'pdf', 'docx')
        ext = os.path.splitext(file_path)[1].lower().replace('.', '')
        
        # 2. Extract text passing both path and ext
        extracted = extract_text(file_path, ext=ext)

        # 3. Handle structure (check if text is a dictionary or a string)
        raw_text = extracted['raw_text'] if isinstance(extracted, dict) else extracted
        structural_issues = extracted.get('structural_issues', []) if isinstance(extracted, dict) else []

        # 4. Run the rest of the parsing steps
        parsed   = parse_resume(raw_text)
        ats      = score_resume(extracted if isinstance(extracted, dict) else {'raw_text': raw_text, 'structural_issues': []}, parsed)
        maritime = maritime_score(raw_text, parsed, pdf_path=file_path)

        return {
            'file':            os.path.basename(file_path),
            'folder':          os.path.basename(os.path.dirname(file_path)),
            'name':            os.path.basename(os.path.dirname(file_path)),  # Use folder name as ground truth
            'email':           parsed['contact'].get('email') or '',
            'phone':           parsed['contact'].get('phone') or '',
            'rank':            maritime['rank_detected'],
            'ats_score':       ats['total_score'],
            'ats_grade':       ats['grade'],
            'maritime_score':  maritime['maritime_score'],
            'maritime_grade':  maritime['maritime_grade'],
            'sea_months':      maritime['sea_months'],
            'cert_coverage':   maritime['cert_coverage'],
            'missing_certs':   ', '.join(maritime['missing_certs']),
            'vessel_types':    ', '.join(maritime['vessel_types']),
            'skills_count':    len(parsed['skills']),
            'issues_count':    len(ats['issues']) + len(maritime['issues']),
            'structural_issues': ', '.join(structural_issues),
            'error':           '',
        }

    except Exception as e:
        return {
            'file':  os.path.basename(file_path) if file_path else "UNKNOWN",
            'name':  'ERROR',
            'error': str(e),
            'ats_score': 0,
            'maritime_score': 0,
        }



def run_batch(list_path: str = 'data/resume_file_list.txt'):
    """Main batch runner. Processes all resumes, saves CSV + JSON."""

    with open(list_path) as f:
        resumes = [l.strip() for l in f if l.strip()]

    # Filter out cover letters from the list
    filtered = []
    for path in resumes:
        basename = os.path.basename(path).lower()
        if any(kw in basename for kw in ['cover', 'letter', 'cl_', '_cl.']):
            continue
        filtered.append(path)
    
    resumes = filtered
    print(f'Starting batch: {len(resumes)} resumes (cover letters filtered)')
    print('='*55)

    results  = []
    failed   = []
    start    = time.time()

    for i, path in enumerate(resumes):
        name = os.path.basename(path)
        print(f'[{i+1}/{len(resumes)}] {name}...', end=' ', flush=True)

        result = process_single(path)

        if result.get('error') and result['name'] in ('ERROR', 'UNREADABLE'):
            print(f'✗ {result["error"]}')
            failed.append(result)
        else:
            print(f'✓ {result["name"]} | ATS:{result["ats_score"]} | Maritime:{result["maritime_score"]}')
            results.append(result)

    # ── Save JSON ────────────────────────────────────────────────────
    os.makedirs('data/outputs', exist_ok=True)
    json.dump(results + failed, open('data/outputs/batch_results.json', 'w'), indent=2)

    # ── Save CSV ─────────────────────────────────────────────────────
    if results:
        keys = [
            'folder', 'name', 'email', 'rank',
            'ats_score', 'ats_grade',
            'maritime_score', 'maritime_grade',
            'sea_months', 'cert_coverage', 'missing_certs',
            'vessel_types', 'skills_count', 'issues_count',
            'structural_issues', 'error'
        ]
        with open('data/outputs/batch_results.csv', 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=keys, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(results)

    elapsed = round(time.time() - start, 1)
    print('\n' + '='*55)
    print(f'BATCH COMPLETE')
    print(f'Processed : {len(results)} succeeded')
    print(f'Failed    : {len(failed)}')
    print(f'Time      : {elapsed}s')
    print(f'Output    : data/outputs/batch_results.csv')
    print('='*55)

    # ── Summary stats ────────────────────────────────────────────────
    if results:
        avg_ats = round(sum(r['ats_score'] for r in results) / len(results), 1)
        avg_mar = round(sum(r['maritime_score'] for r in results) / len(results), 1)
        grades  = {}
        for r in results:
            grades[r['ats_grade']] = grades.get(r['ats_grade'], 0) + 1

        print(f'\nSUMMARY STATS:')
        print(f'Avg ATS Score      : {avg_ats}/100')
        print(f'Avg Maritime Score : {avg_mar}/65')
        print(f'Grade Distribution : {grades}')


if __name__ == '__main__':
    run_batch()