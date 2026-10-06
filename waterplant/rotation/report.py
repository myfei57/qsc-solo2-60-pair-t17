"""Rotation projections and validation rules."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CandidateState:
    """One bed as the rotation evaluator sees it."""

    id: str
    load: float
    production: float
    last_wash: int
    eligible: bool
    readable: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "load": self.load,
            "production": self.production,
            "last_wash": self.last_wash,
            "eligible": self.eligible,
            "readable": self.readable,
        }


@dataclass(frozen=True)
class Decision:
    """The outcome of one rotation evaluation."""

    action: str
    duty: str
    changed: bool
    reason: str
    trigger: str
    ranked: list[str]
    at: int

    def as_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "duty": self.duty,
            "changed": self.changed,
            "reason": self.reason,
            "trigger": self.trigger,
            "ranked": self.ranked,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, payload: object) -> "Decision | None":
        if not isinstance(payload, dict):
            return None
        try:
            ranked = payload["ranked"]
            if not isinstance(ranked, list):
                return None
            return cls(
                action=str(payload["action"]),
                duty=str(payload["duty"]),
                changed=bool(payload["changed"]),
                reason=str(payload["reason"]),
                trigger=str(payload["trigger"]),
                ranked=[str(item) for item in ranked],
                at=int(payload["at"]),
            )
        except (KeyError, TypeError, ValueError):
            return None


@dataclass(frozen=True)
class RotationState:
    """Duty record, policy knobs and the newest decision for the console."""

    duty: str
    duty_since: int
    min_dwell_seconds: float
    load_tolerance: float
    ranked: list[str]
    candidates: list[CandidateState]
    last_decision: Decision | None

    def as_dict(self) -> dict[str, object]:
        return {
            "duty": self.duty,
            "duty_since": self.duty_since,
            "min_dwell_seconds": self.min_dwell_seconds,
            "load_tolerance": self.load_tolerance,
            "ranked": self.ranked,
            "candidates": [candidate.as_dict() for candidate in self.candidates],
            "last_decision": None if self.last_decision is None else self.last_decision.as_dict(),
        }


def validate_production(amount: float) -> None:
    """Reject production increments outside the supported range."""

    if amount < 0:
        raise ValueError("production amount must be non-negative")
    if amount > 1_000_000:
        raise ValueError("production amount exceeds the supported range")


def validate_min_dwell(seconds: float) -> None:
    """Reject dwell windows outside the supported range."""

    if seconds < 0:
        raise ValueError("min dwell must be non-negative")
    if seconds > 1_000_000:
        raise ValueError("min dwell exceeds the supported range")


def validate_tolerance(value: float) -> None:
    """Reject load tolerances outside the supported range."""

    if value < 0:
        raise ValueError("load tolerance must be non-negative")
    if value > 1_000_000:
        raise ValueError("load tolerance exceeds the supported range")
