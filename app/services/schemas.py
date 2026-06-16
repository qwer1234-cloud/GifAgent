"""Pydantic models for validated VLM/LLM structured outputs."""
from pydantic import BaseModel, Field
from typing import Literal

EMOTIONS = Literal[
    "tension", "melancholy", "awe", "joy", "sadness", "catharsis",
    "serenity", "excitement", "dread", "nostalgia", "admiration",
    "intimacy", "vulnerability", "longing", "desire", "other",
]

VALID_EMOTIONS: set[str] = set(EMOTIONS.__args__)


class FrameAnalysis(BaseModel):
    caption: str = Field(min_length=8)
    emotional_core: EMOTIONS
    aesthetic_notes: list[str] = Field(min_length=2, max_length=4)
    why_i_like_it: str = Field(min_length=12)

    @property
    def is_empty(self) -> bool:
        return not self.caption.strip()


class ClipScore(FrameAnalysis):
    gif_worthiness: float = Field(ge=0.0, le=1.0)
    reason: str = Field(min_length=8)


class MediaAnnotation(BaseModel):
    summary: str = Field(min_length=8)
    emotional_core: str = Field(min_length=1)
    aesthetic_notes: list[str] = Field(min_length=1, max_length=6)
    why_i_like_it: str = Field(min_length=12)
    tags: list[str] = Field(min_length=1, max_length=8)
    scene_type: str | None = None
