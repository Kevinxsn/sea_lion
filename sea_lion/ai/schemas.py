"""Validated JSON contracts for model output. Prose never becomes an order; only these fields do."""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

SCHEMA_VERSION = "v1"

EventType = Literal["earnings", "guidance", "m_and_a", "product", "regulatory", "legal", "macro",
                    "analyst", "management", "capital_return", "other"]


class EventFact(BaseModel):
    event_id: str
    ticker: str
    event_type: EventType = "other"
    relevant: bool = True
    sentiment: float = Field(0.0, ge=-1.0, le=1.0)
    importance: float = Field(0.0, ge=0.0, le=1.0)
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    duplicate_of: Optional[str] = None
    one_line: str = Field("", max_length=300)

    @field_validator("ticker")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()


class CheapOutput(BaseModel):
    facts: List[EventFact]


class MainOutput(BaseModel):
    symbol: str
    bull_case: str = Field("", max_length=1200)
    bear_case: str = Field("", max_length=1200)
    horizon_days: int = Field(5, ge=1, le=30)
    impact: float = Field(0.0, ge=-1.0, le=1.0)
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    risk_flags: List[str] = Field(default_factory=list, max_length=10)
    evidence_ids: List[str] = Field(default_factory=list, max_length=20)

    @field_validator("symbol")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("risk_flags")
    @classmethod
    def _flags(cls, v: List[str]) -> List[str]:
        return [str(x)[:60] for x in v]


def json_schema(model: type[BaseModel]) -> dict:
    return model.model_json_schema()
