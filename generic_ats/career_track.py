"""career_track → maritime overlay + JD template mapping.

When career_track is absent or explicitly generic, Layer 2 (maritime) stays
off. Known maritime ranks/tracks (and existing MYC fields target_rank /
target_cruise_line) turn it on. Unrecognized track names stay generic —
we do not invent a maritime overlay for an unrelated industry.
"""

MARITIME_GENERIC = "generic"
MARITIME_NONE = frozenset({"", "generic", "none", "general", "other"})

# Values a caller can send as career_track that mean "run the maritime overlay".
MARITIME_TRACKS = {
    "maritime",
    "seafarer",
    "seafaring",
    "cruise",
    "cruise_line",
    "merchant_navy",
    "deck",
    "engine",
    "eto",
    "deck_officer",
    "chief_officer",
    "second_officer",
    "third_officer",
    "master",
    "chief_engineer",
    "second_engineer",
    "third_engineer",
    "fourth_engineer",
    "engineer",
    "able_seaman",
    "oiler",
    "deck_cadet",
    "engine_cadet",
    "electrical_cadet",
    "rating",
    "bosun",
}

# Rank-style career_track values (spaces or underscores) → MARITIME_JDS key.
# Kept here so generic scoring can pick a JD without importing the rewriter.
TRACK_TO_JD = {
    "maritime": "deck_officer",
    "seafarer": "deck_officer",
    "seafaring": "deck_officer",
    "cruise": "deck_officer",
    "cruise_line": "deck_officer",
    "merchant_navy": "deck_officer",
    "deck": "deck_officer",
    "deck_officer": "deck_officer",
    "master": "deck_officer",
    "chief_officer": "chief_officer",
    "chief officer": "chief_officer",
    "second_officer": "second_officer",
    "second officer": "second_officer",
    "third_officer": "deck_officer",
    "third officer": "deck_officer",
    "engine": "engineer",
    "engineer": "engineer",
    "chief_engineer": "engineer",
    "chief engineer": "engineer",
    "second_engineer": "engineer",
    "second engineer": "engineer",
    "third_engineer": "engineer",
    "third engineer": "engineer",
    "fourth_engineer": "fourth_engineer",
    "fourth engineer": "fourth_engineer",
    "eto": "eto",
    "electro_technical_officer": "eto",
    "electro technical officer": "eto",
    "able_seaman": "able_seaman",
    "able seaman": "able_seaman",
    "ab seaman": "able_seaman",
    "bosun": "able_seaman",
    "oiler": "oiler",
    "deck_cadet": "deck_cadet",
    "deck cadet": "deck_cadet",
    "engine_cadet": "rating",
    "engine cadet": "rating",
    "electrical_cadet": "eto",
    "electrical cadet": "eto",
    "rating": "rating",
}


def _norm(value: str) -> str:
    return (value or "").strip().lower().replace("-", "_")


def maritime_overlay_active(
    career_track: str = "",
    target_rank: str = "",
    target_cruise_line: str = "",
    detected_rank: str = "",
) -> bool:
    """True when Layer 2 (maritime scoring + maritime JD tailoring) should run.

    Explicit generic/none → off.
    target_rank or target_cruise_line → on (existing MYC form fields).
    Known maritime career_track → on.
    Absent career_track: on only if parser already detected a maritime rank,
    so existing seafarer uploads keep maritime scores without a new field.
    """
    if target_rank.strip() or target_cruise_line.strip():
        return True
    track = _norm(career_track)
    if track in MARITIME_NONE:
        if not track and detected_rank and detected_rank.lower() not in ("", "unknown"):
            return True
        return False
    if track in MARITIME_TRACKS or track.replace("_", " ") in TRACK_TO_JD:
        return True
    if track.replace("_", " ") in {
        "chief officer", "second officer", "third officer", "chief engineer",
        "second engineer", "third engineer", "fourth engineer", "able seaman",
        "deck cadet", "engine cadet", "electrical cadet",
    }:
        return True
    return False


def resolve_target_jd(
    career_track: str,
    job_description: str,
    rank: str = "",
    maritime_jds: dict | None = None,
    rank_to_jd: dict | None = None,
) -> str:
    """JD text fed into Resume Matcher's tailoring engine.

    Caller-supplied job_description always wins. Otherwise map career_track
    / rank onto the maritime JD templates. Generic path uses a professional
    baseline (keywords.GENERIC_PROFESSIONAL_JD) when nothing else applies.
    """
    if (job_description or "").strip():
        return job_description.strip()

    from generic_ats.keywords import GENERIC_PROFESSIONAL_JD

    if maritime_jds is None:
        try:
            from services.ai_client import MARITIME_JDS, RANK_TO_JD
            maritime_jds = MARITIME_JDS
            rank_to_jd = RANK_TO_JD
        except Exception:
            maritime_jds = {}
            rank_to_jd = {}
    rank_to_jd = rank_to_jd or {}

    track = _norm(career_track).replace("_", " ")
    track_key = TRACK_TO_JD.get(_norm(career_track)) or TRACK_TO_JD.get(track)
    if track_key and track_key in maritime_jds:
        return maritime_jds[track_key].strip()

    rank_l = (rank or "").strip().lower()
    jd_key = rank_to_jd.get(rank_l)
    if jd_key and jd_key in maritime_jds:
        return maritime_jds[jd_key].strip()

    return GENERIC_PROFESSIONAL_JD.strip()
