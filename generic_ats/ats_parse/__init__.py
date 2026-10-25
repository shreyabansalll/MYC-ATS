"""Deterministic ATS parse-check — from Resume Matcher ats_parse."""

from typing import Literal

from generic_ats.ats_parse.checks import run_checks, score
from generic_ats.ats_parse.extract import ExtractedDocument, document_from_extracted
from generic_ats.ats_parse.models import ParseCheckReport

PREVIEW_CHARACTERS = 2_000


def build_report(
    doc: ExtractedDocument,
    *,
    source: Literal["upload", "render"] = "upload",
    locale: str | None = None,
    template: str | None = None,
    cv_mode: bool = False,
) -> ParseCheckReport:
    checks = run_checks(
        doc, {"locale": locale, "template": template, "cv_mode": cv_mode}
    )
    status = {check.id: check.status for check in checks}
    if status.get("text_layer") == "fail":
        extractability = "none"
    elif (
        status.get("garbled_glyphs") == "fail"
        or status.get("text_coverage", "pass") != "pass"
        or doc.truncated
    ):
        extractability = "partial"
    else:
        extractability = "full"
    return ParseCheckReport(
        source=source,
        extractability=extractability,
        overall_score=score(checks, {"extraction", "layout"}),
        content_score=score(checks, {"content"}),
        page_count=doc.page_count,
        checks=checks,
        template=template,
        extracted_text_preview=doc.text[:PREVIEW_CHARACTERS],
    )


__all__ = ["build_report", "document_from_extracted", "ExtractedDocument", "ParseCheckReport"]
