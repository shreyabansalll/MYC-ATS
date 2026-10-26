# models/candidate_profile.py
# CandidateProfile is the canonical domain object per the architecture
# decision doc: the uploaded resume is an input artifact, generated resumes
# are output artifacts, and business logic operates on CandidateProfile.
# Built from the same `parsed` dict + maritime_score() output that already
# flow between pipeline stages — this just names and types that boundary.

from typing import Optional
from pydantic import BaseModel, Field


class ContactInfo(BaseModel):
    email: Optional[str] = None
    phone: Optional[str] = None
    all_phones: list[str] = Field(default_factory=list)
    linkedin: Optional[str] = None
    github: Optional[str] = None


class CandidateProfile(BaseModel):
    name: str
    contact: ContactInfo
    rank_detected: str
    license_rank: str
    sea_months: int
    certificates: list[str] = Field(default_factory=list)
    missing_certs: list[str] = Field(default_factory=list)
    vessel_types: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    sections: dict[str, str] = Field(default_factory=dict)


def build_candidate_profile(parsed: dict, maritime_result: dict) -> CandidateProfile:
    """
    Constructs the typed CandidateProfile from Stage 2 (parse) and Stage 4
    (maritime score) output. Called once per job, right after those stages,
    before the AI rewrite stage consumes rank/license_rank/sections.

    Pydantic validation here is the boundary check that would have caught
    the maritime re-score bug (Task 1) at write time — if maritime_score()
    ever returned a field with the wrong shape, construction fails loudly
    here instead of a stale/wrong value silently reaching the DB or API.
    """
    return CandidateProfile(
        name=parsed.get('name', 'Unknown'),
        contact=ContactInfo(**parsed.get('contact', {})),
        rank_detected=maritime_result.get('rank_detected', 'unknown'),
        license_rank=maritime_result.get('license_rank', ''),
        sea_months=maritime_result.get('sea_months', 0),
        certificates=maritime_result.get('certificates', []),
        missing_certs=maritime_result.get('missing_certs', []),
        vessel_types=maritime_result.get('vessel_types', []),
        skills=parsed.get('skills', []),
        sections=parsed.get('sections', {}),
    )
