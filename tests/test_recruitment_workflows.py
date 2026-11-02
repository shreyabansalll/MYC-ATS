from pathlib import Path

import pymupdf
from docx import Document
from reportlab.pdfgen import canvas

import pipeline.extractor as extractor
import pipeline.generator as generator
import services.cdc_parser as cdc_parser
from services.recruitment_workflows import (
    build_recruiter_report,
    crew_profile,
    engineering_workflow,
    officer_workflow,
)


def _text_cdc(tmp_path: Path) -> Path:
    path = tmp_path / "authorized_text_cdc.pdf"
    pdf = canvas.Canvas(str(path))
    pdf.drawString(72, 760, "Name: Authorized Sample Candidate")
    pdf.drawString(72, 740, "CDC No: CDC-SAMPLE-001")
    pdf.drawString(72, 720, "M/V Sample Star | Third Officer | 01/2020 - 12/2021")
    pdf.showPage()
    pdf.drawString(72, 760, "M/V Sample Star | Second Officer | 01/2022 - 06/2024")
    pdf.save()
    return path


def test_authorized_text_native_multi_page_cdc_is_parsed_with_page_provenance(tmp_path, monkeypatch):
    monkeypatch.setattr(cdc_parser, "_ocr_pdf_page", lambda *_: {"text": "", "status": "not_needed"})
    cdc = cdc_parser.parse_cdc_pdf(str(_text_cdc(tmp_path)), authorized_sample=True)

    assert cdc["authorized_sample"] is True
    assert cdc["cdc_number"] == "CDC-SAMPLE-001"
    assert len(cdc["pages"]) == 2
    assert len(cdc["sailings"]) == 2
    assert cdc["sailings"][0]["page_number"] == 1
    assert cdc["sailings"][1]["page_number"] == 2
    assert all(sailing["verification_status"] == "verified_authorized_cdc"
               for sailing in cdc["sailings"])


def test_scanned_cdc_uses_page_ocr_and_preserves_manual_verification(tmp_path, monkeypatch):
    path = tmp_path / "authorized_scanned_cdc.pdf"
    document = pymupdf.open()
    page = document.new_page()
    document.save(str(path))
    document.close()

    monkeypatch.setattr(
        cdc_parser,
        "_ocr_pdf_page",
        lambda *_: {"text": "Name: Authorized Scanned Candidate\n"
                            "CDC No: CDC-SCAN-001\n"
                            "M/V Scan Star | Third Officer | 01/2021 - 12/2022",
                     "status": "ok"},
    )
    cdc = cdc_parser.parse_cdc_pdf(
        str(path),
        authorized_sample=True,
        manual_verification_status="pending",
    )

    assert cdc["pages"][0]["extraction_method"] == "ocr"
    assert cdc["pages"][0]["manual_verification_status"] == "pending"
    assert len(cdc["sailings"]) == 1
    assert cdc["sailings"][0]["extraction_method"] == "ocr"


def test_officer_workflow_returns_history_service_certificates_and_application_guidance():
    cdc = {
        "sailings": [
            {"rank": "third officer", "sign_on": "2020-01-01", "sign_off": "2021-12-31",
             "vessel": "Sample Star"},
            {"rank": "second officer", "sign_on": "2022-01-01", "sign_off": "2024-06-30",
             "vessel": "Sample Star"},
        ],
        "last_sailing": {"rank": "second officer", "sign_off": "2024-06-30"},
    }
    result = officer_workflow(
        "Third Officer 01/2020 - 12/2021\n"
        "Second Officer 01/2022 - 06/2024\nGMDSS\nECDIS\nCOC\nSTCW",
        cdc,
        "second officer",
    )
    assert result["workflow"] == "officer"
    assert result["latest_cdc_rank"] == "second officer"
    assert result["rank_history"] == ["second officer", "third officer"]
    assert result["certificates"]["rank"] == "second_officer"
    assert result["reconciliation"]["status"] in {"consistent", "review_required"}


def test_engineer_workflow_keeps_hv_and_responsibility_evidence_separate():
    result = engineering_workflow(
        "Second Engineer\nHV awareness training. Independently maintained main engine generators.\n"
        "Managed the engine room department.\n01/2021 - 12/2023",
        {"sailings": []},
        "second engineer",
    )
    evidence = {item["competency"]: item["evidence_level"] for item in result["competency_evidence"]}
    assert evidence["high_voltage_awareness"] == "mentioned_only"
    assert evidence["independent_maintenance"] == "independently_performed"
    assert evidence["department_management"] == "mentioned_only"
    assert result["hv_requirement"]["status"] == "conditional"


def test_crew_workflow_accepts_land_internship_hotel_and_training_without_coc():
    result = crew_profile(
        "Hotel internship at Ocean Hotel\n"
        "5-star hotel guest service experience\n"
        "Foreign work and training in first aid\nPST PSSR FPFF",
    )
    assert result["profile_type"] == "crew"
    assert result["land_experience"] is True
    assert result["internships"] is True
    assert result["officer_coc_required"] is False
    assert {item["certificate"] for item in result["crew_certificates"]} >= {"PST", "PSSR", "FPFF"}


def test_myc_docx_has_required_order_and_summary_last(tmp_path):
    path = generator.generate_myc_docx({
        "name": "Authorized Sample",
        "rank": "Second Engineer",
        "profile_type": "engineer",
        "technical_expertise": ["Main engine maintenance"],
        "license": ["COC II/2"],
        "documents": ["CDC: authorized"],
        "certifications": ["STCW BST"],
        "education": ["Marine Engineering"],
        "skills": ["Machinery maintenance"],
        "languages": ["English"],
        "experience_bullets": [{
            "role": "Second Engineer", "company": "Sample Shipping",
            "dates": "01/2021 - 12/2023", "bullets": ["Maintained main machinery."],
        }],
        "summary": "Second Engineer with machinery experience.",
    }, "workflow_test")
    doc = Document(path)
    headings = [p.text for p in doc.paragraphs if p.text.upper() == p.text and p.text]
    assert headings.index("TECHNICAL EXPERTISE / CORE COMPETENCIES") < headings.index("LICENSE")
    assert headings.index("LICENSE") < headings.index("DOCUMENTS")
    assert headings.index("PROFESSIONAL EXPERIENCE") < headings.index("SUMMARY")
    assert headings[-1] == "SUMMARY"
    Path(path).unlink()


def test_recruiter_report_contains_original_generated_strengths_changes_and_actions():
    report = build_recruiter_report(
        {"resume_text": "Original resume"},
        {
            "resume_text": "Generated MYC resume",
            "sections_written": ["Certificates", "Professional Experience"],
            "strengths": ["ECDIS demonstrated"],
            "issues": ["Passport expiry date missing"],
            "outstanding_actions": ["Obtain passport renewal evidence"],
        },
    )
    assert report["strengths"] == ["ECDIS demonstrated"]
    assert report["issues_identified"] == ["Passport expiry date missing"]
    assert report["changes_actually_made"]
    assert report["outstanding_actions"] == ["Obtain passport renewal evidence"]
