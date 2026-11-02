"""Run deterministic checks on generated synthetic resumes; no live AI calls."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from docx import Document
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from generic_ats.layer import score_generic
from generic_ats.writing_quality import detect_novel_numbers
from pipeline.extractor import extract_text
from pipeline.maritime import maritime_score
from pipeline.parser import parse_resume


SYNTHETIC_RESUME = """Alex Example
alex.example@example.test
Deck Officer
Summary
Synthetic deck officer profile with bridge watchkeeping experience.
Experience
Deck Officer | Example Maritime Ltd | 2020 - 2024
Coordinated 12 monthly port calls and maintained voyage plans.
Education
Diploma in Nautical Science 2018
Skills
ECDIS, COLREGS, Passage Planning, Bridge Watchkeeping
Certifications
STCW Basic Safety Training
"""
EXPECTED_TEXT_MARKERS = (
    "Alex Example",
    "ECDIS",
    "Diploma in Nautical Science",
    "STCW Basic Safety Training",
)
EXPECTED_SKILLS = ("ECDIS", "COLREGS", "Passage Planning")
SYNTHETIC_JOB_DESCRIPTION = "Deck Officer ECDIS COLREGS passage planning"


def _write_docx(path: Path) -> None:
    document = Document()
    for line in SYNTHETIC_RESUME.splitlines():
        document.add_paragraph(line)
    document.save(path)


def _write_pdf(path: Path) -> None:
    pdf = canvas.Canvas(str(path), pagesize=letter)
    _, height = letter
    for index, line in enumerate(SYNTHETIC_RESUME.splitlines()):
        pdf.drawString(54, height - 54 - index * 18, line)
    pdf.save()


def _evaluate_document(path: Path, ext: str) -> dict:
    extracted = extract_text(str(path), ext)
    raw_text = extracted.get("raw_text", "")
    assert raw_text.strip(), f"{ext.upper()} synthetic document extracted no text"

    normalized_text = " ".join(raw_text.lower().split())
    text_markers_found = sum(
        " ".join(marker.lower().split()) in normalized_text
        for marker in EXPECTED_TEXT_MARKERS
    )
    assert text_markers_found == len(EXPECTED_TEXT_MARKERS), (
        f"{ext.upper()} extraction missed expected synthetic content"
    )

    parsed = parse_resume(raw_text)
    field_checks = {
        "name": parsed.get("name") == "Alex Example",
        "email": parsed.get("contact", {}).get("email") == "alex.example@example.test",
    }
    parsed_skills = " ".join(parsed.get("skills", [])).lower()
    field_checks.update({
        f"skill:{skill}": skill.lower() in parsed_skills
        for skill in EXPECTED_SKILLS
    })
    assert all(field_checks.values()), f"{ext.upper()} field extraction failed: {field_checks}"

    score = score_generic(
        extracted,
        parsed,
        job_description=SYNTHETIC_JOB_DESCRIPTION,
        ext=ext,
    )
    assert 0 <= score["total_score"] <= 100
    assert score["keyword_data"]["match_pct"] >= 75.0
    maritime = maritime_score(raw_text, parsed=parsed)
    assert 0 <= maritime["maritime_score"] <= 85
    return {
        "format": ext,
        "text_markers_found": text_markers_found,
        "text_markers_expected": len(EXPECTED_TEXT_MARKERS),
        "field_checks_passed": sum(field_checks.values()),
        "field_checks_total": len(field_checks),
        "generic_score_synthetic_only": score["total_score"],
        "job_keyword_match_pct_synthetic_only": score["keyword_data"]["match_pct"],
        "maritime_score_synthetic_only": maritime["maritime_score"],
        "maritime_rank_detected_synthetic_only": maritime["rank_detected"],
    }


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="myc-ats-eval-") as temp_dir:
        root = Path(temp_dir)
        docx_path = root / "synthetic_resume.docx"
        pdf_path = root / "synthetic_resume.pdf"
        malformed_pdf_path = root / "malformed.pdf"
        malformed_docx_path = root / "malformed.docx"
        empty_docx_path = root / "empty.docx"

        _write_docx(docx_path)
        _write_pdf(pdf_path)
        malformed_pdf_path.write_bytes(b"%PDF-1.4\nnot a valid PDF")
        malformed_docx_path.write_bytes(b"PK not a valid DOCX archive")
        Document().save(empty_docx_path)

        documents = [
            _evaluate_document(docx_path, "docx"),
            _evaluate_document(pdf_path, "pdf"),
        ]

        malformed_pdf = extract_text(str(malformed_pdf_path), "pdf")
        assert not malformed_pdf.get("raw_text", "").strip()
        assert any(
            issue.startswith("PDF_READ_ERROR:")
            for issue in malformed_pdf.get("structural_issues", [])
        )

        malformed_docx = extract_text(str(malformed_docx_path), "docx")
        assert not malformed_docx.get("raw_text", "").strip()
        assert any(
            issue.startswith("DOCX_READ_ERROR:")
            for issue in malformed_docx.get("structural_issues", [])
        )

        empty_docx = extract_text(str(empty_docx_path), "docx")
        assert not empty_docx.get("raw_text", "").strip()

        unsupported = extract_text(str(root / "unsupported.txt"), "txt")
        assert unsupported.get("structural_issues") == ["UNSUPPORTED_FILE_TYPE"]

        source = "Managed 5 vessels and generated $1.2M annual revenue."
        rewritten = "Increased revenue by 45% and generated $1.2M."
        novel_claims = detect_novel_numbers(source, rewritten)
        assert novel_claims, "Synthetic unsupported percentage claim was not detected"

    print(json.dumps({
        "evaluation": "synthetic deterministic regression checks",
        "live_ai_calls": 0,
        "real_candidate_data_used": False,
        "documents": documents,
        "malformed_pdf_handled": True,
        "malformed_docx_handled": True,
        "empty_docx_handled": True,
        "unsupported_type_handled": True,
        "synthetic_novel_numeric_claim_detected": len(novel_claims),
        "limitations": [
            "Synthetic extraction checks are not a representative accuracy benchmark.",
            "Numeric novelty detection is a narrow heuristic, not general factual verification.",
            "Real-world field accuracy and unsupported-claim rates require authorized labeled data and separately evaluated AI outputs.",
        ],
    }, indent=2))


if __name__ == "__main__":
    main()
