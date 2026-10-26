# main.py
import argparse
import sys
import os
import json
import copy
import logging
import warnings
warnings.filterwarnings('ignore', message='.*FontBBox.*')
logging.getLogger('pdfminer').setLevel(logging.ERROR)

from pipeline.extractor import extract_text, extract_photo
from pipeline.parser import parse_resume
from services.ai_client import restore_protected_signals, rewrite_resume
from pipeline.generator import generate_docx
from models.candidate_profile import build_candidate_profile
from services.two_layer import (
    maritime_stub_for_profile,
    build_protected_signals,
    rewrite_quality_from_improvement,
    should_retry_rewrite,
    score_layers,
    score_layers_after_rewrite,
)


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

    # ── Stage 3: Two-layer ATS scoring ────────────────────────────────
    print('\n[3/6] Scoring resume...')
    layers_before = score_layers(
        extracted,
        parsed,
        job_description=job_description,
        pdf_path=file_path,
        ext=ext,
    )
    ats = layers_before['generic']
    maritime = layers_before['maritime']
    overlay = layers_before['overlay']
    tailored_jd = layers_before['job_description']
    print(f'      ✓  Generic ATS: {ats["total_score"]}/100 — {ats["grade"]}')
    print(f'         Breakdown: Parse:{ats["breakdown"]["parse_check"]}  '
          f'Content:{ats["breakdown"]["content"]}')
    if maritime:
        print(f'      ✓  Maritime   : {maritime["maritime_score"]} — {maritime["maritime_grade"]}')
        print(f'         Rank       : {maritime["rank_detected"]}')
        print(f'         Sea        : {maritime["sea_months"]} months')
        print(f'         Certs      : {maritime["cert_coverage"]}')

    # Typed domain object — canonical candidate record for this job.
    # Downstream stages (rewrite, generate) read from this rather than
    # the loose parsed/maritime dicts at the scoring/rewriting boundary.
    candidate_profile = build_candidate_profile(
        parsed,
        maritime if maritime is not None else maritime_stub_for_profile(parsed),
    )
    protected_signals = build_protected_signals(extracted, parsed, layers_before)
    rank = candidate_profile.rank_detected if overlay else (parsed.get('rank') or '')

    # ── Stage 5: AI Rewrite ──────────────────────────────────────────
    print('\n[5/6] Rewriting resume (Claude)...')
    maritime_issues = maritime['issues'] if maritime else []
    all_issues = ats['issues'] + maritime_issues

    rewritten = rewrite_resume(
        parsed=parsed,
        issues=all_issues,
        job_description=tailored_jd,
        rank=rank,
        license_rank=candidate_profile.license_rank,
        sea_months=candidate_profile.sea_months,
        generic_mode=not overlay,
        protected_signals=protected_signals,
    )

    if 'error' in rewritten:
        print(f'      ✗  Rewriter failed: {rewritten["error"]}')
        print(f'         Detail: {rewritten.get("detail", "")}')
        return {
            'error': 'Rewriter failed',
            'stage': 'ai_rewrite',
            'ats_before': ats['total_score'],
            'maritime': maritime['maritime_score'] if maritime else None,
            'detail': rewritten
        }

    # Inject fields rewriter doesn't carry
    rewritten['name']         = candidate_profile.name
    rewritten['contact']      = candidate_profile.contact.model_dump()
    rewritten['rank']         = rank
    rewritten['sea_months']   = candidate_profile.sea_months
    rewritten['vessel_types'] = candidate_profile.vessel_types

    print(f'      ✓  Rewrite complete')
    print(f'         Issues to fix: {len(rewritten.get("issues_fixed", []))}')

    job_id    = os.path.splitext(os.path.basename(file_path))[0]
    score_before = ats['total_score']

    def generate_and_score(candidate):
        path = generate_docx(copy.deepcopy(candidate), job_id=job_id)
        extracted_after = extract_text(path, 'docx')
        parsed_after = parse_resume(extracted_after['raw_text'])
        layers_after = score_layers_after_rewrite(
            candidate,
            extracted_after,
            parsed_after,
            job_description=tailored_jd,
            overlay=overlay,
        )
        return path, layers_after

    # ── Stage 6: Generate DOCX and verify the rewrite ─────────────────
    print('\n[6/6] Generating ATS-clean DOCX...')
    docx_path, layers_after = generate_and_score(rewritten)
    ats2 = layers_after['generic']
    maritime2 = layers_after['maritime']
    improvement = ats2['total_score'] - score_before

    if should_retry_rewrite(improvement, rewrite_failed=False):
        retry_rewritten = rewrite_resume(
            parsed=parsed,
            issues=all_issues,
            job_description=tailored_jd,
            rank=rank,
            license_rank=candidate_profile.license_rank,
            sea_months=candidate_profile.sea_months,
            generic_mode=not overlay,
            protected_signals=protected_signals,
            strict=True,
        )
        if 'error' not in retry_rewritten:
            retry_rewritten['name'] = candidate_profile.name
            retry_rewritten['contact'] = candidate_profile.contact.model_dump()
            retry_rewritten['rank'] = rank
            retry_rewritten['sea_months'] = candidate_profile.sea_months
            retry_rewritten['vessel_types'] = candidate_profile.vessel_types
            retry_path, retry_layers = generate_and_score(retry_rewritten)
            if retry_layers['generic']['total_score'] > ats2['total_score']:
                rewritten = retry_rewritten
                docx_path = retry_path
                ats2 = retry_layers['generic']
                maritime2 = retry_layers['maritime']
                improvement = ats2['total_score'] - score_before

    score_rollback = ats2['total_score'] < score_before
    if score_rollback:
        sections = parsed.get('sections') or {}
        original_experience = sections.get('experience') or ''
        rewritten = {
            'summary': sections.get('summary', ''),
            'experience_bullets': [{
                'role': rank or 'Professional',
                'company': '',
                'dates': '',
                'bullets': [line.strip() for line in original_experience.splitlines() if line.strip()],
            }] if original_experience.strip() else [],
            'skills': list(parsed.get('skills') or []),
            'certifications': [
                line.strip() for line in (sections.get('certifications') or '').splitlines()
                if line.strip()
            ],
            'education': [
                line.strip() for line in (sections.get('education') or '').splitlines()
                if line.strip()
            ],
            'documents': {},
            'languages': [
                line.strip() for line in (sections.get('languages') or '').splitlines()
                if line.strip()
            ],
            'technical_expertise': [
                line.strip() for line in (sections.get('technical_expertise') or '').splitlines()
                if line.strip()
            ],
            'issues_fixed': ['AI rewrite reduced the ATS score; original content preserved'],
            'rank': rank,
        }
        rewritten = restore_protected_signals(rewritten, parsed, protected_signals)
        rewritten['name'] = candidate_profile.name
        rewritten['contact'] = candidate_profile.contact.model_dump()
        rewritten['sea_months'] = candidate_profile.sea_months
        rewritten['vessel_types'] = candidate_profile.vessel_types
        docx_path, layers_after = generate_and_score(rewritten)
        ats2 = layers_after['generic']
        maritime2 = layers_after['maritime']
        improvement = ats2['total_score'] - score_before

    rewrite_quality = (
        'none'
        if score_rollback
        else 'partial'
        if improvement == 0
        else rewrite_quality_from_improvement(improvement)
    )
    ai_rewrite = not score_rollback

    # The optional photo variant is generated only from the selected
    # non-degrading rewrite (or restored source content).
    photo_bytes = extract_photo(file_path) if ext == 'pdf' else None
    docx_photo_path = None
    if photo_bytes:
        docx_photo_path = generate_docx(
            copy.deepcopy(rewritten), job_id=job_id, include_photo=True, photo_bytes=photo_bytes,
        )
    score_after = ats2['total_score']
    print(f'      ✓  DOCX saved: {docx_path}')
    print(f'\n  ┌─────────────────────────────────────────┐')
    print(f'  │  ATS Score before      : {score_before:3d}/100 {ats["grade"]:<10}│')
    print(f'  │  ATS Score after       : {score_after:3d}/100 {ats2["grade"]:<10}│')
    print(f'  │  Improvement           : {improvement:+4d} pts               │')
    if maritime and maritime2:
        print(f'  │  Maritime Score before : {maritime["maritime_score"]:3d} {maritime["maritime_grade"]:<10}    │')
        print(f'  │  Maritime Score after  : {maritime2["maritime_score"]:3d} {maritime2["maritime_grade"]:<10}    │')
    print(f'  └─────────────────────────────────────────┘')

    # ── Final Report ─────────────────────────────────────────────────
    return {
        'candidate': {
            'name':    candidate_profile.name,
            'contact': candidate_profile.contact.model_dump(),
            'rank':    rank,
            'skills':  candidate_profile.skills,
        },
        'scores': {
            'ats_before':      score_before,
            'ats_after':       score_after,
            'improvement':     improvement,
            'rewrite_quality': rewrite_quality,
            'ai_rewrite':      ai_rewrite,
            'grade_before':    ats['grade'],
            'grade_after':     ats2['grade'],
            'maritime_before':       maritime['maritime_score'] if maritime else None,
            'maritime_grade_before': maritime['maritime_grade'] if maritime else None,
            'maritime_after':        maritime2['maritime_score'] if maritime2 else None,
            'maritime_grade_after':  maritime2['maritime_grade'] if maritime2 else None,
            # Back-compat aliases — historically pointed at the pre-rewrite value
            'maritime':        maritime['maritime_score'] if maritime else None,
            'maritime_grade':  maritime['maritime_grade'] if maritime else None,
        },
        'maritime': {
            'sea_months':    maritime['sea_months'] if maritime else 0,
            'vessel_types':  maritime['vessel_types'] if maritime else [],
            'cert_coverage': maritime['cert_coverage'] if maritime else None,
            'missing_certs': maritime['missing_certs'] if maritime else [],
        },
        'output': {
            'docx_path':       docx_path,
            'docx_photo_path': docx_photo_path,
        },
        'issues_fixed':    rewritten.get('issues_fixed', []),
        'issues_remaining': ats2['issues'],
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Score and rewrite a seafarer resume.'
    )
    parser.add_argument('resume_path', help='Path to a PDF or DOCX resume')
    parser.add_argument(
        'job_description',
        nargs='?',
        default='',
        help='Optional job description for keyword scoring',
    )
    args = parser.parse_args()
    file_path = args.resume_path
    job_desc = args.job_description
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
    print(f'\nOutput DOCX (photo-free): {report["output"]["docx_path"]}')
    if report['output']['docx_photo_path']:
        print(f'Output DOCX (with photo): {report["output"]["docx_photo_path"]}')
    else:
        print(f'Output DOCX (with photo): none — no extractable photo in source')
    if report['issues_fixed']:
        print(f'\nISSUES FIXED ({len(report["issues_fixed"])}):')
        for i in report['issues_fixed']:
            print(f'  ✓ {i}')
    if report['issues_remaining']:
        print(f'\nSTILL PRESENT ({len(report["issues_remaining"])}):')
        for i in report['issues_remaining'][:5]:
            print(f'  • {i}')
    print('='*55)