"""Validated, consent-referenced records for isolated offline learning datasets."""
from __future__ import annotations
from enum import Enum
from pydantic import BaseModel, Field, model_validator


class FeedbackKind(str, Enum):
    recruiter_correction = 'recruiter_correction'
    extraction_correction = 'extraction_correction'
    certificate_correction = 'certificate_correction'
    rank_correction = 'rank_correction'
    rewrite_preference = 'rewrite_preference'
    vacancy_match = 'vacancy_match'
    recruiter_ranking = 'recruiter_ranking'


class FeedbackRecord(BaseModel):
    schema_version: int = 1
    kind: FeedbackKind
    source_ref: str = Field(min_length=8, max_length=128, pattern=r'^[A-Za-z0-9_-]+$')
    consent_record_ref: str = Field(min_length=8, max_length=128, pattern=r'^[A-Za-z0-9_-]+$')
    reviewer_ref: str = Field(min_length=8, max_length=128, pattern=r'^[A-Za-z0-9_-]+$')
    correction: dict = Field(default_factory=dict)
    preferred: str | None = Field(default=None, max_length=12000)
    rejected: str | None = Field(default=None, max_length=12000)
    split_group: str = Field(min_length=8, max_length=128, pattern=r'^[A-Za-z0-9_-]+$')

    @model_validator(mode='after')
    def check_pair(self):
        if self.kind == FeedbackKind.rewrite_preference and (not self.preferred or not self.rejected):
            raise ValueError('rewrite preference requires both preferred and rejected examples')
        if self.preferred and self.rejected and self.preferred == self.rejected:
            raise ValueError('preferred and rejected examples must differ')
        return self


class RankingFeedbackRecord(BaseModel):
    schema_version: int = 1
    vacancy_ref: str = Field(min_length=8, max_length=128, pattern=r'^[A-Za-z0-9_-]+$')
    candidate_ref: str = Field(min_length=8, max_length=128, pattern=r'^[A-Za-z0-9_-]+$')
    reviewer_ref: str = Field(min_length=8, max_length=128, pattern=r'^[A-Za-z0-9_-]+$')
    consent_record_ref: str = Field(min_length=8, max_length=128, pattern=r'^[A-Za-z0-9_-]+$')
    relevance: int = Field(ge=0, le=3)
    features: dict[str, float]
    split_group: str = Field(min_length=8, max_length=128, pattern=r'^[A-Za-z0-9_-]+$')

    @model_validator(mode='after')
    def finite_features(self):
        import math
        if not self.features or any(not math.isfinite(value) for value in self.features.values()):
            raise ValueError('ranking feedback requires finite numeric features')
        return self


def assert_split_isolation(train: list, test: list) -> None:
    overlap = {item.split_group for item in train} & {item.split_group for item in test}
    if overlap:
        raise ValueError('source groups must not leak across train/test splits')
