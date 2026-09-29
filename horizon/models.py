from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from math import isfinite
from typing import Protocol


def probability(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError("Probability must be a number")
    if not isfinite(value) or not 0 <= value <= 1:
        raise ValueError("Probability outside [0, 1]")
    return float(value)


def aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Explicit timezone required")
    return value


@dataclass(frozen=True)
class Question:
    id: str
    text: str
    criteria: str
    as_of: datetime
    deadline: datetime
    kind: str = "binary"
    domain: str = "other"
    units: str | None = None
    fine_print: str = ""
    background: str = ""
    close_time: datetime | None = None

    def __post_init__(self):
        if aware(self.deadline) <= aware(self.as_of):
            raise ValueError("Forecast must precede deadline")
        if not self.text.strip() or not self.criteria.strip():
            raise ValueError("Question and resolution criteria required")
        if self.close_time is not None and aware(self.close_time) <= self.as_of:
            raise ValueError("Question is closed for forecasting")

    @property
    def criteria_hash(self):
        return sha256((self.criteria + "\n" + self.fine_print).encode()).hexdigest()


@dataclass(frozen=True)
class QuestionAnalysis:
    domain: str
    horizon_days: float
    criteria_hash: str
    units: str | None
    checks: tuple[str, ...]


@dataclass(frozen=True)
class Evidence:
    id: str
    text: str
    source: str
    published_at: datetime | None
    available_at: datetime
    primary: bool = False
    reliability: float = 0.5
    relevance: float = 0.5
    direction: str = "neutral"
    origin_id: str | None = None

    def __post_init__(self):
        if self.published_at is not None:
            aware(self.published_at)
        aware(self.available_at)
        probability(self.reliability)
        probability(self.relevance)
        if not self.id or not self.source or not self.text.strip():
            raise ValueError("Evidence needs identity, source and text")
        if self.direction not in {"yes", "no", "neutral"}:
            raise ValueError("Invalid direction")


@dataclass(frozen=True)
class Signal:
    probability: float
    available_at: datetime
    source: str
    rationale: str

    def __post_init__(self):
        probability(self.probability)
        aware(self.available_at)
        if not self.source or not self.rationale:
            raise ValueError("Signal needs provenance and rationale")


@dataclass(frozen=True)
class AgentForecast:
    agent: str
    probability: float
    confidence: float
    drivers: tuple[str, ...]
    counterargument: str
    crux: str
    model: str
    prompt_version: str = "council-v1"

    def __post_init__(self):
        probability(self.probability)
        probability(self.confidence)
        if not self.agent or not self.model or not self.drivers or not self.counterargument or not self.crux:
            raise ValueError("Incomplete forecast")


@dataclass
class ForecastRecord:
    question: Question
    analysis: QuestionAnalysis
    agents: list[AgentForecast]
    evidence: list[Evidence]
    base_rate: Signal | None
    market: Signal | None
    raw: float | None
    calibrated: float | None
    disagreement: float
    cruxes: list[str]
    status: str
    events: list[dict] = field(default_factory=list)
    outcome: int | None = None
    calibration_version: str = "identity-v1"
    research: dict | None = None
    resolution_analysis: dict | None = None
    red_team: dict | None = None
    schema_version: int = 2
    decision_codes: list[str] = field(default_factory=list)
    adjudication: dict | None = None


class HistoricalRetriever(Protocol):
    def retrieve(self, question: Question) -> list[Signal]: ...


class Calibrator(Protocol):
    version: str
    def transform(self, value: float) -> float: ...


class IdentityCalibration:
    version = "identity-v1"
    def transform(self, value: float) -> float:
        return probability(value)
