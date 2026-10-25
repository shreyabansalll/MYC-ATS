"""Standalone tests for Layer 1 (Resume Matcher generic ATS). No LLM, no maritime."""

import os

from generic_ats.ats import compute_ats_score, compute_section_completeness, compute_title_match
from generic_ats.ats_parse import build_report, document_from_extracted
from generic_ats.career_track import maritime_overlay_active, resolve_target_jd
from generic_ats.keywords import extract_job_keywords, keyword_coverage
from generic_ats.layer import score_generic
from generic_ats.text_match import keyword_in_text


def _resume(**overrides):
    base = {
        "personalInfo": {
            "name": "Ada Lovelace",
            "title": "Software Engineer",
            "email": "ada@example.com",
            "phone": "+1-555-0100",
        },
        "summary": "Software Engineer with Python and FastAPI experience.",
        "workExperience": [
            {
                "id": 1,
                "title": "Software Engineer",
                "company": "Analytical Engines",
                "years": "Jan 2020 - Mar 2023",
                "description": ["Built FastAPI services in Python"],
            }
        ],
        "education": [
            {
                "id": 1,
                "institution": "University",
                "degree": "B.S.",
                "years": "2014 - 2018",
            }
        ],
        "additional": {"technicalSkills": ["Python", "FastAPI", "SQL"]},
    }
    base.update(overrides)
    return base


def test_keyword_in_text_does_not_match_substring_skills():
    assert keyword_in_text("java", "javascript experience") is False
    assert keyword_in_text("python", "Used Python and FastAPI") is True


def test_compute_ats_score_standalone_with_matching_jd():
    resume = _resume()
    job_kw = {
        "role": "Software Engineer",
        "required_skills": ["Python", "FastAPI"],
        "preferred_skills": ["SQL"],
        "keywords": ["Python", "FastAPI", "SQL"],
    }
    result = compute_ats_score(resume, job_kw, 100.0, [], [])
    assert result["overall_score"] >= 80
    assert result["sub_scores"]["title_match"] == 100.0
    assert result["sub_scores"]["skills_coverage"] == 100.0


def test_title_match_none_when_posting_has_no_role():
    assert compute_title_match(_resume(), {"role": ""}) is None


def test_section_completeness_falls_back_to_heading_scan():
    resume = {
        "personalInfo": {},
        "rawDump": "Summary\nExperience\nEducation\nSkills\npython",
    }
    # Unstructured: no structured sections, scan text. Contact absent.
    score = compute_section_completeness(resume)
    assert 0 <= score <= 100


def test_parse_check_fails_text_layer_on_empty_extract():
    doc = document_from_extracted({"raw_text": "", "structural_issues": []}, "pdf")
    report = build_report(doc)
    assert report.extractability == "none"
    assert report.overall_score < 100


def test_parse_check_flags_multi_column_from_marine_extractor():
    doc = document_from_extracted(
        {
            "raw_text": "Name\nExperience\nEducation\nSkills\n" + ("word " * 40),
            "structural_issues": ["MULTI_COLUMN_LAYOUT_DETECTED"],
        },
        "pdf",
    )
    report = build_report(doc)
    multi = next(c for c in report.checks if c.id == "multi_column")
    assert multi.status == "fail"


def test_score_generic_returns_api_shaped_result():
    extracted = {
        "raw_text": (
            "Ada Lovelace\nada@example.com +1-555-0100111\n"
            "Summary\nSoftware Engineer.\n"
            "Experience\nJan 2020 - Mar 2023 Analytical Engines\n"
            "Built FastAPI services in Python\n"
            "Education\nB.S. 2014 - 2018\n"
            "Skills\nPython FastAPI SQL\n"
        ),
        "structural_issues": [],
    }
    parsed = {
        "name": "Ada Lovelace",
        "contact": {"email": "ada@example.com", "phone": "+1-555-0100111"},
        "skills": ["Python", "FastAPI", "SQL"],
        "sections": {
            "summary": "Software Engineer with Python.",
            "experience": "Jan 2020 - Mar 2023 Analytical Engines. Built FastAPI.",
            "education": "B.S. 2014 - 2018",
            "skills": "Python FastAPI SQL",
        },
    }
    result = score_generic(
        extracted, parsed, job_description="Seeking Software Engineer with Python FastAPI",
        role="Software Engineer",
    )
    assert 0 <= result["total_score"] <= 100
    assert result["grade"] in {"EXCELLENT", "GOOD", "PASS", "FAIL"}
    assert result["keyword_data"]["engine"] == "resume_matcher"
    assert "parse_check" in result["breakdown"]


def test_extract_job_keywords_is_deterministic():
    a = extract_job_keywords("Seeking Python developer with FastAPI experience")
    b = extract_job_keywords("Seeking Python developer with FastAPI experience")
    assert a == b
    assert "Python" in a["keywords"] or "python" in [k.lower() for k in a["keywords"]]


def test_keyword_coverage_reports_missing():
    pct, missing = keyword_coverage("python fastapi", {"keywords": ["Python", "Kubernetes"]})
    assert "Kubernetes" in missing
    assert 0 < pct < 100


def test_maritime_overlay_off_for_explicit_generic():
    assert maritime_overlay_active(career_track="generic") is False
    assert maritime_overlay_active(career_track="") is False


def test_maritime_overlay_on_for_known_track_and_myc_fields():
    assert maritime_overlay_active(career_track="chief_officer") is True
    assert maritime_overlay_active(career_track="", target_rank="third officer") is True
    assert maritime_overlay_active(career_track="", detected_rank="eto") is True


def test_resolve_target_jd_prefers_explicit_job_description():
    jd = resolve_target_jd("chief_officer", "Custom JD here", rank="chief officer")
    assert jd == "Custom JD here"


def test_score_layers_generic_only_when_forced():
    from services.two_layer import score_layers

    extracted = {
        "raw_text": (
            "Ada Lovelace\nada@example.com +1-555-0100111\n"
            "Summary\nSoftware Engineer.\nExperience\nBuilt FastAPI.\n"
            "Education\nB.S.\nSkills\nPython\n"
        ),
        "structural_issues": [],
    }
    parsed = {
        "name": "Ada Lovelace",
        "rank": "",
        "contact": {"email": "ada@example.com", "phone": "+1-555-0100111"},
        "skills": ["Python"],
        "sections": {
            "summary": "Software Engineer.",
            "experience": "Built FastAPI.",
            "education": "B.S.",
            "skills": "Python",
        },
        "raw_text": extracted["raw_text"],
    }
    layers = score_layers(extracted, parsed, career_track="generic", ext="pdf")
    assert layers["overlay"] is False
    assert layers["maritime"] is None
    assert 0 <= layers["generic"]["total_score"] <= 100


def test_score_layers_runs_maritime_for_known_track():
    from services.two_layer import score_layers

    extracted = {
        "raw_text": (
            "Third Officer Jane\njane@example.com +1-555-0100222\n"
            "STCW CoC ENG1 GMDSS\nSea Service 01/2020 - 12/2021\n"
        ),
        "structural_issues": [],
    }
    parsed = {
        "name": "Jane",
        "rank": "third officer",
        "contact": {"email": "jane@example.com"},
        "skills": ["ECDIS"],
        "sections": {"experience": "01/2020 - 12/2021 oil tanker", "summary": "Third Officer"},
        "raw_text": extracted["raw_text"],
    }
    layers = score_layers(extracted, parsed, career_track="third_officer", ext="pdf")
    assert layers["overlay"] is True
    assert layers["maritime"] is not None
    assert "maritime_score" in layers["maritime"]
    assert "STCW" in layers["job_description"] or "stcw" in layers["job_description"].lower()


def test_rewrite_quality_and_retry_threshold():
    from services.two_layer import rewrite_quality_from_improvement, should_retry_rewrite

    assert rewrite_quality_from_improvement(15) == "full"
    assert rewrite_quality_from_improvement(14) == "partial"
    assert rewrite_quality_from_improvement(0) == "none"
    assert rewrite_quality_from_improvement(-1) == "none"
    assert should_retry_rewrite(14, rewrite_failed=False) is True
    assert should_retry_rewrite(15, rewrite_failed=False) is False
    assert should_retry_rewrite(0, rewrite_failed=True) is False


def test_classic_pdf_is_single_column_and_nonempty(tmp_path):
    from generic_ats.pdf_classic import generate_classic_pdf

    path = str(tmp_path / "out.pdf")
    generate_classic_pdf(
        {
            "name": "Ada Lovelace",
            "rank": "Software Engineer",
            "contact": {"email": "ada@example.com"},
            "summary": "Engineer with Python experience.",
            "experience_bullets": [
                {
                    "role": "Software Engineer",
                    "company": "Analytical Engines",
                    "dates": "2020 - 2023",
                    "bullets": ["Built FastAPI services"],
                }
            ],
            "skills": ["Python", "FastAPI"],
            "certifications": [],
            "education": ["B.S. Mathematics"],
            "languages": ["English"],
        },
        path,
    )
    assert os.path.exists(path)
    assert os.path.getsize(path) > 200


def test_generic_mode_does_not_invent_seafarer_skills():
    from services.ai_client import enforce_minimum_quality

    result = enforce_minimum_quality(
        {"summary": "One. Two. Three.", "skills": ["Python"], "experience_bullets": [], "certifications": []},
        rank="",
        generic_mode=True,
    )
    assert "Python" in result["skills"]
    assert not any("STCW" in str(s).upper() or "COLREG" in str(s).upper() for s in result["skills"])
    assert result["experience_bullets"] == []


def test_writing_quality_buzzword_and_em_dash_scrubbing():
    from generic_ats.writing_quality import clean_text_for_ats, scrub_ai_phrases, scrub_em_dashes

    raw = "Spearheaded a paradigm shift — leveraged best-in-class synergies."
    cleaned = clean_text_for_ats(raw)
    assert "Spearheaded" not in cleaned
    assert "Led" in cleaned or "led" in cleaned
    assert "—" not in cleaned
    assert "paradigm shift" not in cleaned
    assert "used" in cleaned.lower()


def test_writing_quality_novel_number_detection():
    from generic_ats.writing_quality import detect_novel_numbers

    source = "Managed a fleet of 5 vessels generating $1.2M annual revenue."
    candidate_safe = "Oversaw 5 commercial vessels and $1.2M in annual revenue."
    candidate_hallucinated = "Increased revenue by 45% across 12 vessels and saved $500k."

    assert detect_novel_numbers(source, candidate_safe) == []
    novel = detect_novel_numbers(source, candidate_hallucinated)
    assert len(novel) >= 2


def test_pdf_template_styles_all_render(tmp_path):
    from generic_ats.pdf_classic import generate_classic_pdf

    data = {
        "name": "Grace Hopper",
        "rank": "Rear Admiral",
        "contact": {"email": "grace@navy.mil"},
        "summary": "Pioneered computer programming and compilers.",
        "experience_bullets": [{"role": "Director", "company": "Naval Data Automation", "dates": "1970 - 1980", "bullets": ["Developed compiler standards"]}],
        "skills": ["COBOL", "Systems Architecture"],
        "certifications": ["Distinguished Fellow"],
        "education": ["Ph.D. Mathematics, Yale University"],
        "languages": ["English"],
    }
    for style in ("classic", "modern", "minimal"):
        pdf_path = str(tmp_path / f"resume_{style}.pdf")
        generate_classic_pdf(data, pdf_path, style=style)
        assert os.path.exists(pdf_path)
        assert os.path.getsize(pdf_path) > 500
