"""Extract explicitly stated certificate dates without assuming validity periods."""

from __future__ import annotations

import re
from calendar import monthrange
from datetime import date, datetime

from rules.cert_rules import CERT_MATRIX, CERT_UNIVERSAL_REQUIREMENTS, _aliases_for_cert

_DATE = re.compile(
    r"(?P<d>\d{1,2})[/-](?P<m>\d{1,2})[/-](?P<y>\d{4})|"
    r"(?P<month>\d{1,2})[/-](?P<year>\d{4})|"
    r"(?P<iso>20\d{2}-\d{2}-\d{2})|"
    r"(?P<name>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
    r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?)\s+(?P<name_year>\d{4})",
    re.I,
)
_EXPIRY = re.compile(r"\b(expir(?:y|es|ed)?|valid\s+(?:until|till|through))\b", re.I)
_ISSUE = re.compile(r"\b(issued|issue\s+date|date\s+of\s+issue)\b", re.I)
_AMBIGUOUS = re.compile(r"\b(?:valid|expiry|expires?)\b", re.I)
_CATALOG = sorted({
    cert for entry in CERT_MATRIX.values()
    for key in ("mandatory_certs", "preferred_certs", "conditional_certs")
    for cert in entry.get(key, [])
} | set(CERT_UNIVERSAL_REQUIREMENTS), key=len, reverse=True)
_MONTHS = {name.casefold(): index for index, name in enumerate(
    ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"), 1
)}


def _parse_date(match: re.Match) -> tuple[date | None, bool]:
    try:
        if match.group("iso"):
            return date.fromisoformat(match.group("iso")), False
        if match.group("name"):
            month = next(value for name, value in _MONTHS.items() if match.group("name").casefold().startswith(name[:3]))
            return date(int(match.group("name_year")), month, 1), True
        if match.group("month"):
            month = int(match.group("month"))
            return date(int(match.group("year")), month, 1), True
        # Slash dates are interpreted as DD/MM/YYYY for Indian resumes.
        return date(int(match.group("y")), int(match.group("m")), int(match.group("d"))), False
    except (ValueError, StopIteration):
        return None, False


def _serialize(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _safe_evidence(line: str) -> str:
    value = line.strip()[:300]
    value = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "[redacted email]", value)
    value = re.sub(r"(?<!\w)\+?\d[\d\s()./-]{7,}\d(?!\w)", "[redacted number]", value)
    return re.sub(
        r"\b(passport|cdc|indos|document|certificate)\s*(?:no\.?|number|#|:)\s*[A-Z0-9/-]+",
        r"\1 [redacted]",
        value,
        flags=re.I,
    )


def track_certificate_validity(
    resume_text: str,
    *,
    today: date | None = None,
    expiring_soon_days: int = 183,
) -> list[dict]:
    """Return evidence-linked date statuses for certificates found in text.

    No certificate-specific duration is inferred. A month-only expiry is treated
    as the end of that month; ambiguous or missing dates remain explicit.
    """
    if expiring_soon_days < 0:
        raise ValueError("expiring_soon_days must be non-negative")
    today = today or datetime.utcnow().date()
    results = []
    for certificate in _CATALOG:
        aliases = _aliases_for_cert(certificate)
        for line in (resume_text or "").splitlines():
            if not any(re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", line, re.I) for alias in aliases):
                continue
            dates = []
            expiry_match = _EXPIRY.search(line)
            for match in _DATE.finditer(line):
                parsed, month_only = _parse_date(match)
                if parsed:
                    if month_only and expiry_match and match.start() >= expiry_match.end():
                        parsed = date(parsed.year, parsed.month, monthrange(parsed.year, parsed.month)[1])
                    dates.append((match, parsed))
            issue_match = _ISSUE.search(line)
            expiry = None
            issue = None
            if expiry_match:
                later = [(m, d) for m, d in dates if m.start() >= expiry_match.end()]
                expiry = later[0][1] if later else None
            if issue_match:
                following = [(m, d) for m, d in dates if m.start() >= issue_match.end()]
                issue = following[0][1] if following else None

            ambiguous = bool(_AMBIGUOUS.search(line) and not expiry and not dates)
            if expiry:
                days = (expiry - today).days
                status = "EXPIRED" if days < 0 else "EXPIRING_SOON" if days <= expiring_soon_days else "VALID"
            elif ambiguous:
                status = "DATE_AMBIGUOUS"
            elif dates and expiry_match:
                status = "REQUIRES_VERIFICATION"
            else:
                status = "EXPIRY_UNKNOWN"
            results.append({
                "certificate": certificate,
                "evidence": _safe_evidence(line),
                "issue_date": _serialize(issue),
                "expiry_date": _serialize(expiry),
                "status": status,
                "confidence": "high" if expiry or issue else "low",
                "note": "Document date evidence only; no underlying qualification validity or universal validity period is inferred.",
            })
            break
    return results
