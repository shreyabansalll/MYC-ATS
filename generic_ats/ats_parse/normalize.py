"""Text normalization — from Resume Matcher ats_parse/normalize.py."""

import re
import unicodedata

_HTML_TAG = re.compile(r"</?(?:strong|em|u|a)\b[^>]*>", re.IGNORECASE)
_HYPHEN_BREAK = re.compile(r"(\w)-\n\s*(\w)")
_LETTER_SPACED = re.compile(r"(?<!\S)(?:\w )+\w(?!\S)")
_TOKEN = re.compile(r"[\w.+-]{1,64}@[\w-]+(?:\.[\w-]+)+|\w+", re.UNICODE)
_CJK = re.compile(r"([぀-ヿ㐀-鿿가-힯\U00020000-\U0003FFFF])")


def _join_letters(match: re.Match[str]) -> str:
    run = match.group(0)
    return run.replace(" ", "") if len(run) >= 5 else run


def rejoin_letter_spacing(text: str) -> str:
    return _LETTER_SPACED.sub(_join_letters, text)


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = _HTML_TAG.sub("", text)
    text = _HYPHEN_BREAK.sub(r"\1-\2", text)
    text = "\n".join(rejoin_letter_spacing(line) for line in text.splitlines())
    return re.sub(r"[ \t ]+", " ", text).casefold().strip()
