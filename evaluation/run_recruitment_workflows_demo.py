from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.cdc_parser import parse_cdc_pdf
from services.recruitment_workflows import (
    build_recruiter_report,
    crew_profile,
    engineering_workflow,
    officer_workflow,
)


def run_demo(cdc_path: str | None = None) -> dict:
    officer_resume = (
        "Third Officer\nM/V Example Star | 01/2022 - 12/2023\n"
        "GMDSS GOC\nECDIS\n"
    )
    engineer_resume = (
        "Second Engineer\nHigh voltage awareness and assisted maintenance of main engine and generators.\n"
        "01/2021 - 12/2023\n"
    )
    cdc = parse_cdc_pdf(cdc_path) if cdc_path else {
        "status": "not_supplied",
        "sailings": [],
        "last_sailing": None,
    }
    report_original = {"resume_text": officer_resume}
    generated = {
        "resume_text": "Generated MYC resume",
        "docx_path": "demo.docx",
        "sections_written": ["Technical Expertise", "License", "Documents", "Certificates",
                             "Education", "Skills and Languages", "Professional Experience", "Summary"],
        "strengths": ["ECDIS and GMDSS evidence"],
        "issues": ["CDC not supplied"],
        "outstanding_actions": ["Verify CDC sailings and certificate expiry dates."],
    }
    return {
        "officer": officer_workflow(officer_resume, cdc, "third officer"),
        "engineering_eto": engineering_workflow(engineer_resume, cdc, "second engineer"),
        "crew": crew_profile("Hotel internship; guest interaction; PST and PSSR training."),
        "recruiter_report": build_recruiter_report(report_original, generated),
        "synthetic_only": not cdc_path,
    }


if __name__ == "__main__":
    print(json.dumps(run_demo(), indent=2))
