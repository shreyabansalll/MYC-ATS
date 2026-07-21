# main.py
import sys
import os
import json
import logging
import warnings
warnings.filterwarnings('ignore', message='.*FontBBox.*')
logging.getLogger('pdfminer').setLevel(logging.ERROR)

from pipeline.extractor import extract_text
from pipeline.parser import parse_resume
from pipeline.scorer import score_resume
from pipeline.maritime_scorer import maritime_score
from pipeline.ai_rewriter import rewrite_resume
from pipeline.generator import generate_docx


def run_pipeline(file_path: str, job_description: str = '') -> dict:
    ext = file_path.split('.')[-1].lower()

    # ── Stage 1: Extract ─────────────────────────────────────────────
    print(f'\n[1/6] Extracting text...')
    extracted = extract_text(file_path, ext)

    if not extracted['raw_text']:
        return {'error': 'Could not extract text', 'details': extracted}

    if extracted['structural_issues']:
        print(f'      ⚠  Issues: {extracted["structural_issues"]}')
        print(f'      ⚠  Column severity: {extracted.get("column_severity", "N/A")}')
    else:
        print('      ✓  Clean single-column layout')

    # ── Stage 2: Parse ───────────────────────────────────────────────
    print('\n[2/6] Parsing resume...')
    parsed = parse_resume(extracted['raw_text'])
    parsed['raw_text'] = extracted['raw_text'] 
    print(f'      ✓  Name    : {parsed["name"]}')
    print(f'      ✓  Email   : {parsed["contact"]["email"]}')
    print(f'      ✓  Skills  : {len(parsed["skills"])} found')

    # ── Stage 3: ATS Score ───────────────────────────────────────────
    print('\n[3/6] ATS scoring...')
    ats = score_resume(extracted, parsed, job_description)
    print(f'      ✓  ATS Score: {ats["total_score"]}/100 — {ats["grade"]}')
    print(f'         Breakdown: Format:{ats["breakdown"]["format"]}  '
          f'Sections:{ats["breakdown"]["sections"]}  '
          f'Keywords:{ats["breakdown"]["keywords"]}')

    # ── Stage 4: Maritime Score ──────────────────────────────────────
    print('\n[4/6] Maritime scoring...')
    maritime = maritime_score(extracted['raw_text'], parsed, pdf_path=file_path)
    print(f'      ✓  Maritime : {maritime["maritime_score"]} — {maritime["maritime_grade"]}')
    print(f'         Rank     : {maritime["rank_detected"]}')
    print(f'         Sea      : {maritime["sea_months"]} months')
    print(f'         Certs    : {maritime["cert_coverage"]}')

    # ── Stage 5: AI Rewrite ──────────────────────────────────────────
    print('\n[5/6] Rewriting resume (Groq)...')
    all_issues = ats['issues'] + maritime['issues']

    # In run_pipeline(), change Stage 5 call:
    rewritten = rewrite_resume(
    parsed=parsed,
    issues=all_issues,
    job_description=job_description,
    rank=maritime['rank_detected'],
    license_rank=maritime.get('license_rank', ''),   # ← add this line
    )

    if 'error' in rewritten:
        print(f'      ✗  Rewriter failed: {rewritten["error"]}')
        print(f'         Detail: {rewritten.get("detail", "")}')
        return {
            'error': 'Rewriter failed',
            'stage': 'ai_rewrite',
            'ats_before': ats['total_score'],
            'maritime': maritime['maritime_score'],
            'detail': rewritten
        }

    # Inject fields rewriter doesn't carry
    rewritten['name']    = parsed['name']
    rewritten['contact'] = parsed['contact']
    rewritten['rank']    = maritime['rank_detected']

    print(f'      ✓  Rewrite complete')
    print(f'         Issues to fix: {len(rewritten.get("issues_fixed", []))}')

    # ── Stage 6: Generate DOCX ───────────────────────────────────────
    print('\n[6/6] Generating ATS-clean DOCX...')
    job_id    = os.path.splitext(os.path.basename(file_path))[0]
    docx_path = generate_docx(rewritten, job_id=job_id)

    # Re-score to verify improvement
    extracted2 = extract_text(docx_path, 'docx')
    parsed2    = parse_resume(extracted2['raw_text'])
    ats2       = score_resume(extracted2, parsed2, job_description)
    # No PDF to read for the regenerated DOCX — maritime_score() falls back
    # to text-based sea-months extraction from parsed2's sections.
    maritime2  = maritime_score(extracted2['raw_text'], parsed2)

    score_before = ats['total_score']
    score_after  = ats2['total_score']
    improvement  = score_after - score_before

    print(f'      ✓  DOCX saved: {docx_path}')
    print(f'\n  ┌─────────────────────────────────────────┐')
    print(f'  │  ATS Score before      : {score_before:3d}/100 {ats["grade"]:<10}│')
    print(f'  │  ATS Score after       : {score_after:3d}/100 {ats2["grade"]:<10}│')
    print(f'  │  Improvement           : {improvement:+4d} pts               │')
    print(f'  │  Maritime Score before : {maritime["maritime_score"]:3d} {maritime["maritime_grade"]:<10}    │')
    print(f'  │  Maritime Score after  : {maritime2["maritime_score"]:3d} {maritime2["maritime_grade"]:<10}    │')
    print(f'  └─────────────────────────────────────────┘')

    # ── Final Report ─────────────────────────────────────────────────
    return {
        'candidate': {
            'name':    parsed['name'],
            'contact': parsed['contact'],
            'rank':    maritime['rank_detected'],
            'skills':  parsed['skills'],
        },
        'scores': {
            'ats_before':      score_before,
            'ats_after':       score_after,
            'improvement':     improvement,
            'grade_before':    ats['grade'],
            'grade_after':     ats2['grade'],
            'maritime_before':       maritime['maritime_score'],
            'maritime_grade_before': maritime['maritime_grade'],
            'maritime_after':        maritime2['maritime_score'],
            'maritime_grade_after':  maritime2['maritime_grade'],
            # Back-compat aliases — historically pointed at the pre-rewrite value
            'maritime':        maritime['maritime_score'],
            'maritime_grade':  maritime['maritime_grade'],
        },
        'maritime': {
            'sea_months':    maritime['sea_months'],
            'vessel_types':  maritime['vessel_types'],
            'cert_coverage': maritime['cert_coverage'],
            'missing_certs': maritime['missing_certs'],
        },
        'output': {
            'docx_path':    docx_path,
        },
        'issues_fixed':    rewritten.get('issues_fixed', []),
        'issues_remaining': ats2['issues'],
    }


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('Usage: venv/bin/python main.py <resume_path> [job_description]')
        sys.exit(1)

    file_path = sys.argv[1]
    job_desc  = sys.argv[2] if len(sys.argv) > 2 else ''
    report    = run_pipeline(file_path, job_desc)

    if 'error' in report:
        print(f'\n✗ Pipeline failed at stage: {report.get("stage", "unknown")}')
        print(f'  {report["error"]}')
        sys.exit(1)

    print('\n' + '='*55)
    print('FINAL REPORT — Marine Your Career ATS System')
    print('='*55)
    print(f'Candidate    : {report["candidate"]["name"]}')
    print(f'Rank         : {report["candidate"]["rank"].title()}')
    print(f'Sea Service  : {report["maritime"]["sea_months"]} months')
    print(f'Vessel Types : {", ".join(report["maritime"]["vessel_types"])}')
    print(f'Cert Coverage: {report["maritime"]["cert_coverage"]}')
    print(f'\nATS Before   : {report["scores"]["ats_before"]}/100 — {report["scores"]["grade_before"]}')
    print(f'ATS After    : {report["scores"]["ats_after"]}/100  — {report["scores"]["grade_after"]}')
    print(f'Improvement  : {report["scores"]["improvement"]:+d} pts')
    print(f'Maritime Before : {report["scores"]["maritime_before"]} — {report["scores"]["maritime_grade_before"]}')
    print(f'Maritime After  : {report["scores"]["maritime_after"]} — {report["scores"]["maritime_grade_after"]}')
    print(f'\nOutput DOCX  : {report["output"]["docx_path"]}')
    if report['issues_fixed']:
        print(f'\nISSUES FIXED ({len(report["issues_fixed"])}):')
        for i in report['issues_fixed']:
            print(f'  ✓ {i}')
    if report['issues_remaining']:
        print(f'\nSTILL PRESENT ({len(report["issues_remaining"])}):')
        for i in report['issues_remaining'][:5]:
            print(f'  • {i}')
    print('='*55)