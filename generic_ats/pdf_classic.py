"""Classic and Modern single-column ATS PDF templates — ReportLab implementation.

Adheres to Resume Matcher ATS-safe layout rules without requiring headless Chromium:
- 100% single-column flow (no floating text boxes, multi-column tables, or sidebars)
- Clean, standard typography (Helvetica / Helvetica-Bold)
- Standard margins (0.75 in)
- Clear ALL-CAPS section headings with distinct separators
- Safe characters only (no em dashes or corrupt glyphs)
- Selectable template styles: 'classic', 'modern', 'minimal'
"""

from __future__ import annotations

import os
import re
from typing import Any

from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen import canvas

_XML_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _safe(text: Any) -> str:
    s = _XML_ILLEGAL.sub("", str(text or ""))
    # Replace em-dashes and non-ASCII dashes with standard ASCII hyphen
    return s.replace("—", " - ").replace("–", " - ")


def _as_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    return [value] if value else []


# Theme color schemes for templates
THEMES = {
    "classic": {
        "header_rgb": (0.18, 0.46, 0.71),  # Classic Marine/Navy Blue
        "line_rgb": (0.18, 0.46, 0.71),
        "line_width": 0.8,
        "name_size": 16,
        "body_size": 10,
        "leading": 13,
    },
    "modern": {
        "header_rgb": (0.12, 0.20, 0.35),  # Deep Midnight Slate
        "line_rgb": (0.35, 0.45, 0.55),
        "line_width": 0.5,
        "name_size": 17,
        "body_size": 10,
        "leading": 13,
    },
    "minimal": {
        "header_rgb": (0.10, 0.10, 0.10),  # Pure Dark Charcoal
        "line_rgb": (0.70, 0.70, 0.70),
        "line_width": 0.4,
        "name_size": 16,
        "body_size": 10,
        "leading": 13,
    },
}


class _Pager:
    def __init__(self, path: str, style: str = "classic"):
        self.c = canvas.Canvas(path, pagesize=letter)
        self.width, self.height = letter
        self.left = 0.75 * inch
        self.right = self.width - 0.75 * inch
        self.bottom = 0.6 * inch
        self.y = self.height - 0.65 * inch
        self.theme = THEMES.get(style, THEMES["classic"])
        self.c.setTitle("ATS Resume")
        self.c.setAuthor("Marine ATS")

    def newline(self, size: float):
        self.y -= size * 1.25
        if self.y < self.bottom:
            self.c.showPage()
            self.y = self.height - 0.65 * inch

    def ensure(self, need: float):
        if self.y - need < self.bottom:
            self.c.showPage()
            self.y = self.height - 0.65 * inch

    def draw_wrapped(self, text: str, font: str, size: float, leading: float | None = None):
        text = _safe(text)
        if not text:
            return
        leading = leading or size + 3
        max_width = self.right - self.left
        words = text.split()
        line = ""
        for word in words:
            trial = (line + " " + word).strip()
            if pdfmetrics.stringWidth(trial, font, size) <= max_width:
                line = trial
            else:
                self.ensure(leading)
                self.c.setFont(font, size)
                self.c.drawString(self.left, self.y, line)
                self.y -= leading
                line = word
        if line:
            self.ensure(leading)
            self.c.setFont(font, size)
            self.c.drawString(self.left, self.y, line)
            self.y -= leading

    def heading(self, text: str):
        self.ensure(22)
        self.y -= 8
        self.c.setFont("Helvetica-Bold", 11)
        r, g, b = self.theme["header_rgb"]
        self.c.setFillColorRGB(r, g, b)
        self.c.drawString(self.left, self.y, _safe(text).upper())
        self.y -= 3
        lr, lg, lb = self.theme["line_rgb"]
        self.c.setStrokeColorRGB(lr, lg, lb)
        self.c.setLineWidth(self.theme["line_width"])
        self.c.line(self.left, self.y, self.right, self.y)
        self.c.setFillColorRGB(0, 0, 0)
        self.y -= 12

    def save(self):
        self.c.save()


def generate_classic_pdf(content: dict, path: str, style: str = "classic") -> str:
    """Write an ATS-safe single-column PDF.

    Args:
        content: Resume dict with summary, experience_bullets, skills, etc.
        path: Target file path to write.
        style: Theme style ('classic', 'modern', 'minimal').

    Returns:
        The written file path.
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    p = _Pager(path, style=style)

    name = _safe(content.get("name") or "Candidate")
    p.c.setFont("Helvetica-Bold", p.theme["name_size"])
    p.c.drawString(p.left, p.y, name)
    p.y -= 16

    rank = _safe(content.get("rank") or "")
    if rank:
        p.c.setFont("Helvetica", 11)
        p.c.drawString(p.left, p.y, rank.title())
        p.y -= 14

    contact = content.get("contact") or {}
    bits = [
        _safe(contact.get("email")),
        _safe(contact.get("phone")),
        _safe(contact.get("location")),
        _safe(contact.get("linkedin")),
    ]
    contact_line = "  |  ".join(b for b in bits if b)
    if contact_line:
        p.draw_wrapped(contact_line, "Helvetica", 9)
        p.y -= 4

    summary = _safe(content.get("summary"))
    if summary:
        p.heading("Professional Summary")
        p.draw_wrapped(summary, "Helvetica", p.theme["body_size"], leading=p.theme["leading"])

    experience = _as_list(content.get("experience_bullets"))
    if experience:
        p.heading("Work Experience")
        for job in experience:
            if isinstance(job, str):
                p.draw_wrapped("• " + _safe(job), "Helvetica", p.theme["body_size"])
                continue
            if not isinstance(job, dict):
                continue
            role = _safe(job.get("role"))
            company = _safe(job.get("company"))
            dates = _safe(job.get("dates"))
            # ATS safe delimiter: " - " instead of em-dash " — "
            header = " - ".join(x for x in (role, company) if x)
            if dates:
                header = f"{header}  |  {dates}" if header else dates
            if header:
                p.draw_wrapped(header, "Helvetica-Bold", 10)
            for bullet in _as_list(job.get("bullets")):
                p.draw_wrapped("• " + _safe(bullet), "Helvetica", p.theme["body_size"], leading=p.theme["leading"])
            p.y -= 4

    skills = [str(s) for s in _as_list(content.get("skills")) if str(s).strip()]
    if skills:
        p.heading("Skills")
        p.draw_wrapped(", ".join(skills), "Helvetica", p.theme["body_size"], leading=p.theme["leading"])

    certs = _as_list(content.get("certifications"))
    if certs:
        p.heading("Certifications")
        for c in certs:
            p.draw_wrapped("• " + _safe(c if isinstance(c, str) else str(c)), "Helvetica", p.theme["body_size"])

    education = _as_list(content.get("education"))
    if education:
        p.heading("Education")
        for row in education:
            p.draw_wrapped("• " + _safe(row if isinstance(row, str) else str(row)), "Helvetica", p.theme["body_size"])

    languages = _as_list(content.get("languages"))
    if languages:
        p.heading("Languages")
        p.draw_wrapped(", ".join(_safe(x) for x in languages), "Helvetica", p.theme["body_size"])

    p.save()
    return path
