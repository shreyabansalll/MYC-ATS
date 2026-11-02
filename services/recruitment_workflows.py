from __future__ import annotations

import re
from datetime import date

from services.maritime_certificates import certificate_requirement_resolution
from services.rank_intelligence import evaluate_rank_suitability, normalize_rank
from services.sea_service import calculate_sea_service
from services.two_layer import check_rank_eligibility


def _rank_slug(value: str) -> str:
    rank = normalize_rank(value)
    return rank or (value or "").strip().casefold()


def reconcile_sources(resume_text: str, cdc: dict) -> dict:
    flags = []
    resume_service = calculate_sea_service(resume_text)
    resume_ranks = {
        key[5:].replace("_", " ")
        for key in resume_service["aggregates"]
        if key.startswith("rank:")
    }
    cdc_sailings = cdc.get("sailings", [])
    cdc_ranks = {s.get("rank") for s in cdc_sailings if s.get("rank")}
    for rank in cdc_ranks - resume_ranks:
        flags.append({
            "type": "rank_conflict_or_missing_resume_evidence",
            "details": f"CDC contains {rank}; resume does not establish it.",
            "severity": "review",
        })
    seen = set()
    duplicates = []
    for sailing in cdc_sailings:
        key = (sailing.get("sign_on"), sailing.get("sign_off"), sailing.get("rank"))
        if key in seen:
            duplicates.append(key)
        seen.add(key)
    if duplicates:
        flags.append({"type": "duplicate_cdc_voyages", "details": duplicates, "severity": "review"})
    if cdc.get("last_sailing") and not cdc["sailings"]:
        flags.append({"type": "last_sailing_missing_detail", "details": "Manual review required.", "severity": "review"})
    return {
        "status": "review_required" if flags else "consistent",
        "flags": flags,
        "resume_ranks": sorted(resume_ranks),
        "cdc_ranks": sorted(cdc_ranks),
        "source_policy": "Never silently select a source because it produces a higher score.",
    }


def officer_workflow(resume_text: str, cdc: dict, rank: str = "") -> dict:
    rank = _rank_slug(rank)
    service = calculate_sea_service("\n".join([
        resume_text,
        *(f"{s.get('rank') or ''} | {s.get('vessel') or ''} | "
          f"{s.get('sign_on') or ''} - {s.get('sign_off') or ''}"
          for s in cdc.get("sailings", [])),
    ]))
    certificates = certificate_requirement_resolution(rank, resume_text)
    reconcile = reconcile_sources(resume_text, cdc)
    application = check_rank_eligibility(rank, None, rank)
    latest_rank = cdc.get("last_sailing", {}).get("rank") if cdc.get("last_sailing") else None
    return {
        "workflow": "officer",
        "rank_history": sorted(
            {s.get("rank") for s in cdc.get("sailings", []) if s.get("rank")},
        ),
        "last_sailing": cdc.get("last_sailing"),
        "latest_cdc_rank": latest_rank,
        "rank_specific_sea_service": {
            key: value for key, value in service["aggregates"].items() if key.startswith("rank:")
        },
        "sea_service": service["total"],
        "certificates": certificates,
        "reconciliation": reconcile,
        "application_rank": application,
    }


def engineering_workflow(resume_text: str, cdc: dict, rank: str) -> dict:
    officer = officer_workflow(resume_text, cdc, rank)
    text = (resume_text or "").casefold()
    competency_phrases = {
        "high_voltage_awareness": "high voltage awareness" in text and "hv awareness" in text,
        "high_voltage_authorization": bool(re.search(r"\bhv\s+(?:authorized|authorised|switching)\b", text)),
        "independent_maintenance": bool(re.search(r"\bindependently?\s+(?:maintained|maintenanc|troubleshot)\b", text)),
        "department_management": bool(re.search(r"\b(?:managed|supervised)\s+(?:engine room|engineering department)\b", text)),
    }
    evidenced = []
    for term, found in competency_phrases.items():
        evidence_level = (
            "independently_performed" if found and term in {"independent_maintenance", "high_voltage_authorization"}
            else "mentioned_only"
        )
        evidenced.append({"competency": term, "evidenced": found, "evidence_level": evidence_level})
    return {
        **officer,
        "workflow": "engineer_eto",
        "competency_evidence": evidenced,
        "hv_requirement": {
            "status": "conditional",
            "basis": "Apply only where the source vessel or vacancy requires HV-capable systems.",
        },
    }


def crew_profile(resume_text: str) -> dict:
    text = resume_text or ""
    hotel_hits = re.findall(
        r"(?:hotel|cruise hotel)\s+([A-Za-z][A-Za-z &.'-]{2,60})",
        text,
        re.I,
    )
    def section(name: str) -> bool:
        return bool(re.search(rf"\b{name}\b", text, re.I))
    crew_certs = []
    for cert in ("PST", "PSSR", "FPFF", "EFA", "STCW Basic Safety Training"):
        if re.search(rf"\b{re.escape(cert)}\b", text, re.I):
            crew_certs.append({"certificate": cert, "status": "evidenced_unverified"})
    return {
        "workflow": "crew",
        "profile_type": "crew",
        "land_experience": section("hotel|hospitality|land"),
        "internships": section("internship|trainee"),
        "hotel_experience": [name.strip() for name in hotel_hits if name.strip()],
        "crew_certificates": crew_certs,
        "officer_coc_required": False,
        "sections": {
            "training": bool(section("training")),
            "foreign_work": bool(section("foreign|international")),
            "guest_interaction": bool(section("guest|customer")),
        },
        "note": "Officer-level COC rules do not apply to this crew profile.",
    }


def build_recruiter_report(original: dict, generated: dict) -> dict:
    original_text = str(original.get("resume_text", "")).strip()
    generated_text = str(generated.get("resume_text", "")).strip()
    strengths = generated.get("strengths", [])
    changes = []
    if original_text and generated_text and original_text != generated_text:
        changes.append("Structured and generated an MYC-format DOCX resume.")
    for section in generated.get("sections_written", []):
        changes.append(f"Prepared {section} section.")
    return {
        "report_type": "recruiter_report",
        "strengths": strengths,
        "issues_identified": generated.get("issues", []),
        "changes_actually_made": changes,
        "outstanding_actions": generated.get("outstanding_actions", []),
        "original_preserved": bool(original),
        "generated_resume_present": bool(generated_text or generated.get("docx_path")),
        "privacy": "Sensitive document numbers are excluded unless explicitly authorized.",
    }
