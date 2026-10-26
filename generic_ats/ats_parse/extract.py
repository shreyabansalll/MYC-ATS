"""Build an ExtractedDocument from marine-ats extractor output.

Resume Matcher's extract.py uses pdfminer layout boxes. This adapter maps
the already-extracted marine-ats dict (pdfplumber structural flags) onto
the same Check-facing schema so ats_parse/checks.py can run unchanged.
"""

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class TextBox:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str


@dataclass
class PageLayout:
    width: float
    height: float
    boxes: list[TextBox]
    images: int = 0

    @property
    def text(self) -> str:
        return "\n".join(box.text for box in self.boxes)


@dataclass
class ExtractedDocument:
    kind: Literal["pdf", "docx"]
    text: str
    page_count: int | None = None
    truncated: bool = False
    pages: list[PageLayout] = field(default_factory=list)
    images: int = 0
    tables: int = 0
    columns: int = 1
    header_footer_text: str = ""
    textbox_text: str = ""
    known_multi_column: bool = False


def document_from_extracted(extracted: dict, ext: str = "pdf") -> ExtractedDocument:
    issues = extracted.get("structural_issues") or []
    joined = " ".join(str(i) for i in issues)
    kind: Literal["pdf", "docx"] = "docx" if ext.lower() == "docx" else "pdf"
    multi = "MULTI_COLUMN" in joined
    images = 1 if ("IMAGE" in joined or "GRAPHICS" in joined) else 0
    tables = 1 if "TABLE" in joined else 0
    header_bits = []
    if "HEADER" in joined:
        header_bits.append("header")
    if "FOOTER" in joined:
        header_bits.append("footer")
    return ExtractedDocument(
        kind=kind,
        text=extracted.get("raw_text") or "",
        page_count=extracted.get("page_count"),
        images=images,
        tables=tables,
        columns=2 if multi else 1,
        header_footer_text=" ".join(header_bits),
        known_multi_column=multi,
    )
