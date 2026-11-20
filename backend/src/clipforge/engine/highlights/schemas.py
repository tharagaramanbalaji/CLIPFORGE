"""What the LLM must return (validated with Pydantic) and the internal types around it."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, Field


def _clip(n: int) -> AfterValidator:
    """Over-long text is truncated, not rejected: a re-ask for a long rationale would double plan usage."""
    return AfterValidator(lambda s: s if len(s) <= n else s[: n - 1].rstrip() + "…")


Text100 = Annotated[str, _clip(100)]
Text80 = Annotated[str, _clip(80)]
Text300 = Annotated[str, _clip(300)]
Text600 = Annotated[str, _clip(600)]


class Category(StrEnum):
    FUNNY = "funny"
    REACTION = "reaction"
    OPINION = "opinion"
    DEBATE = "debate"
    EMOTIONAL = "emotional"
    SURPRISING = "surprising"
    EDUCATIONAL = "educational"
    STORY = "story"
    EXCHANGE = "exchange"
    GAMING = "gaming"
    OTHER = "other"


DIMENSIONS = ("hook", "emotion", "standalone", "story", "shareability")


class DimensionScores(BaseModel):
    hook: int = Field(ge=0, le=10)
    emotion: int = Field(ge=0, le=10)
    standalone: int = Field(ge=0, le=10)
    story: int = Field(ge=0, le=10)
    shareability: int = Field(ge=0, le=10)


class LlmCandidate(BaseModel):
    start_segment: int
    end_segment: int
    category: Category
    hook: Text300
    description: Text600
    why_interesting: Text600
    standalone_context: Literal["self_contained", "needs_light_setup", "needs_prior_context"]
    setup_segment: int | None = None
    scores: DimensionScores


class WindowResult(BaseModel):
    candidates: list[LlmCandidate] = Field(default_factory=list)


class Refinement(BaseModel):
    keep: bool
    start_segment: int
    end_segment: int
    standalone_context: Literal["self_contained", "needs_light_setup", "needs_prior_context"]
    scores: DimensionScores
    title: Text100
    hook_text: Text80  # short on-screen text
    description: Text300
    rationale: Text600


class JudgeItem(BaseModel):
    id: int
    keep: bool
    category: Category = Category.GAMING
    title: Text100 = ""
    hook_text: Text80 = ""
    scores: DimensionScores
    reason: Text300 = ""


class JudgeResult(BaseModel):
    moments: list[JudgeItem] = Field(default_factory=list)


# --- internal ----------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Seg:
    """Transcript segment as the highlight engine sees it (from transcript_segments)."""

    idx: int
    start_ms: int
    end_ms: int
    text: str
    speaker: str | None
    words: tuple[tuple[int, int], ...]  # (start_ms, end_ms) per word
    low_confidence: bool = False


@dataclass
class Candidate:
    """A candidate as it moves through validation, dedupe, refinement and ranking."""

    window_idx: int
    start_segment: int
    end_segment: int
    category: Category
    hook: str
    description: str
    why_interesting: str
    standalone_context: str
    scores: dict[str, int]
    start_ms: int = 0
    end_ms: int = 0
    setup_segment: int | None = None
    errors: list[str] = field(default_factory=list)
    status: str = "proposed"  # proposed | rejected_validation | merged | refined | discarded
    merged_from: list[int] = field(default_factory=list)  # indexes of merged-away candidates
    title: str | None = None
    hook_text: str | None = None
    final_score: float | None = None
    rank: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms
