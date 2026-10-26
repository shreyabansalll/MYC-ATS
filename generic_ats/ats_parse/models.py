"""Parse-check report schema — from Resume Matcher ats_parse/models.py."""

from typing import Any, Literal

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.0"

Category = Literal["extraction", "layout", "content"]
Severity = Literal["high", "medium", "low"]
Status = Literal["pass", "warn", "fail"]


class Check(BaseModel):
    id: str
    category: Category
    severity: Severity
    status: Status
    params: dict[str, Any] = Field(default_factory=dict)


class ParseCheckReport(BaseModel):
    schema_version: str = SCHEMA_VERSION
    source: Literal["upload", "render"]
    extractability: Literal["full", "partial", "none"]
    overall_score: int
    content_score: int
    page_count: int | None
    checks: list[Check]
    template: str | None = None
    extracted_text_preview: str
