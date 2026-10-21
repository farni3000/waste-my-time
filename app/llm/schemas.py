from __future__ import annotations

from pydantic import BaseModel, Field


class LLMRecommendation(BaseModel):
    candidate_id: int
    rank: int = 0
    reason: str
    match_tags: list[str] = Field(default_factory=list)
    confidence: float = 0.5


class LLMResponse(BaseModel):
    recommendations: list[LLMRecommendation] = Field(default_factory=list)
