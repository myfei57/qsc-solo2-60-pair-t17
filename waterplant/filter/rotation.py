"""Duty rotation that balances current load, production and wash history."""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

from .bank import Bank
from .bed import Bed

# Production is deliberately a per-backwash-cycle counter, not a shift counter.
PRODUCTION_CYCLE = "backwash"
# Running arrangement for close loads: give the bed that has waited longest a turn.
TIE_BREAKER = "last_backwash"
LOAD_TIE_TOLERANCE = 0.10
MIN_DUTY_INTERVAL_SECONDS = 1800


class BackwashReader(Protocol):
    def active(self) -> list[str]: ...
    def last_finish(self, bed_id: str) -> int: ...


class AuditorLike(Protocol):
    def record(self, kind: str, detail: str) -> object: ...


@dataclass(frozen=True)
class RotationPolicy:
    """Fixed operating rules for duty rotation."""

    load_tie_tolerance: float = LOAD_TIE_TOLERANCE
    min_duty_interval_seconds: int = MIN_DUTY_INTERVAL_SECONDS
    production_cycle: str = PRODUCTION_CYCLE
    tie_breaker: str = TIE_BREAKER


@dataclass(frozen=True)
class Candidate:
    bed_id: str
    load: float
    cycle_production: float
    last_backwash: int
    last_duty: int
    incumbent: bool
    cooled_down: bool


@dataclass(frozen=True)
class RotationResult:
    on_duty: str | None
    previous: str | None
    order: list[str]
    reason: str
    changed: bool = False
    idempotent: bool = False
    available: bool = True
    policy: RotationPolicy = field(default_factory=RotationPolicy)

    def as_dict(self) -> dict[str, object]:
        return {
            "on_duty": self.on_duty or "",
            "previous": self.previous or "",
            "order": self.order,
            "reason": self.reason,
            "changed": self.changed,
            "idempotent": self.idempotent,
            "available": self.available,
            "production_cycle": self.policy.production_cycle,
            "tie_breaker": self.policy.tie_breaker,
        }


class RotationService:
    """Select duty using all bed factors and record every actual change."""

    def __init__(
        self,
        bank: Bank,
        backwash: BackwashReader,
        auditor: AuditorLike,
        clock: Callable[[], int] | None = None,
        policy: RotationPolicy | None = None,
    ) -> None:
        self._bank = bank
        self._backwash = backwash
        self._auditor = auditor
        self._clock = clock or (lambda: int(time.time()))
        self._policy = policy or RotationPolicy()
        self._last_result: RotationResult | None = None

    def last_result(self) -> RotationResult | None:
        return self._last_result

    def rotate(self, readings: dict[str, float | None] | None = None) -> RotationResult:
        current = self._bank.on_duty()
        try:
            now = int(self._clock())
            active = set(self._backwash.active())
            candidates = self._read_candidates(now, active, readings or {})
        except (OSError, TypeError, ValueError) as exc:
            return self._remember(
                RotationResult(
                    on_duty=current,
                    previous=current,
                    order=[],
                    reason=f"rotation state unavailable: {exc}; duty kept unchanged",
                    idempotent=True,
                    available=False,
                    policy=self._policy,
                )
            )

        if not candidates:
            return self._remember(
                RotationResult(
                    on_duty=None,
                    previous=current,
                    order=[],
                    reason="no eligible filter bed; all beds are closed or backwashing",
                    idempotent=current is None,
                    policy=self._policy,
                )
            )

        order = [candidate.bed_id for candidate in self._rank(candidates)]
        incumbent = next((candidate for candidate in candidates if candidate.incumbent), None)
        if incumbent is not None:
            near_limit = self._highest_load(candidates) * (1.0 - self._policy.load_tie_tolerance)
            if incumbent.load >= near_limit:
                reason = self._reason_for(
                    incumbent, incumbent, not incumbent.cooled_down, active
                )
                return self._remember(
                    RotationResult(
                        on_duty=current,
                        previous=current,
                        order=order,
                        reason=reason,
                        idempotent=True,
                        policy=self._policy,
                    )
                )

        ranked = self._rank(
            [candidate for candidate in candidates if candidate.cooled_down]
        )
        if not ranked:
            return self._remember(
                RotationResult(
                    on_duty=current,
                    previous=current,
                    order=order,
                    reason="no cooled-down eligible filter bed; duty kept unchanged",
                    idempotent=True,
                    policy=self._policy,
                )
            )
        selected = ranked[0]
        reason = self._reason_for(selected, incumbent, False, active)

        if selected.bed_id == current:
            return self._remember(
                RotationResult(
                    on_duty=current,
                    previous=current,
                    order=order,
                    reason=reason,
                    idempotent=True,
                    policy=self._policy,
                )
            )

        detail = {
            "from": current or "",
            "to": selected.bed_id,
            "reason": reason,
            "load": selected.load,
            "cycle_production": selected.cycle_production,
            "last_backwash": selected.last_backwash,
            "production_cycle": self._policy.production_cycle,
            "tie_breaker": self._policy.tie_breaker,
        }
        with self._bank.locked():
            self._auditor.record("rotation", json.dumps(detail, ensure_ascii=False))
            self._bank.rotate(selected.bed_id, now=now)

        return self._remember(
            RotationResult(
                on_duty=selected.bed_id,
                previous=current,
                order=order,
                reason=reason,
                changed=True,
                policy=self._policy,
            )
        )

    def _remember(self, result: RotationResult) -> RotationResult:
        self._last_result = result
        return result

    def _read_candidates(
        self, now: int, active: set[str], supplied_readings: dict[str, float | None]
    ) -> list[Candidate]:
        candidates: list[Candidate] = []
        for bed in self._bank.beds():
            recorded_finish = self._backwash.last_finish(bed.id)
            if bed.closed or bed.id in active:
                continue
            load = self._read_load(bed, supplied_readings)
            candidates.append(
                Candidate(
                    bed_id=bed.id,
                    load=load,
                    cycle_production=bed.cycle_production,
                    last_backwash=max(bed.last_backwash, recorded_finish),
                    last_duty=bed.last_duty,
                    incumbent=bed.duty,
                    cooled_down=(
                        bed.last_duty == 0
                        or now - bed.last_duty >= self._policy.min_duty_interval_seconds
                    ),
                )
            )
        return candidates

    def _read_load(self, bed: Bed, supplied_readings: dict[str, float | None]) -> float:
        if supplied_readings:
            if bed.id not in supplied_readings:
                raise ValueError(f"missing current load for bed {bed.id}")
            value = supplied_readings[bed.id]
        else:
            value = bed.load
        if value is None or not math.isfinite(value) or value < 0:
            raise ValueError(f"unreadable current load for bed {bed.id}")
        return value

    def _near(self, candidates: list[Candidate]) -> list[Candidate]:
        near_limit = self._highest_load(candidates) * (1.0 - self._policy.load_tie_tolerance)
        return [candidate for candidate in candidates if candidate.load >= near_limit]

    def _highest_load(self, candidates: list[Candidate]) -> float:
        return max(candidate.load for candidate in candidates)

    def _rank(self, candidates: list[Candidate]) -> list[Candidate]:
        if not candidates:
            return []
        near_limit = self._highest_load(candidates) * (1.0 - self._policy.load_tie_tolerance)
        near = self._near(candidates)
        far = [candidate for candidate in candidates if candidate.load < near_limit]
        near.sort(
            key=lambda candidate: (
                1 if candidate.incumbent else 0,
                candidate.last_backwash,
                candidate.cycle_production,
                candidate.bed_id,
            )
        )
        far.sort(key=lambda candidate: (-candidate.load, candidate.bed_id))
        return near + far

    def _reason_for(
        self,
        selected: Candidate,
        incumbent: Candidate | None,
        held_for_cooldown: bool,
        active: set[str],
    ) -> str:
        excluded = f"; excluded backwashing={sorted(active)}" if active else ""
        factors = (
            f"load={selected.load:.4f}, cycle_production={selected.cycle_production:.4f}, "
            f"last_backwash={selected.last_backwash}{excluded}"
        )
        if incumbent is None:
            return (
                f"initial duty selected by current load, older last backwash and "
                f"lower cycle production; {factors}"
            )
        if held_for_cooldown:
            return f"recent duty cooldown prevented re-selection; {factors}"
        if selected.bed_id == incumbent.bed_id:
            return (
                f"incumbent retained; load within tie tolerance "
                f"{self._policy.load_tie_tolerance:.2f}; {factors}"
            )
        if selected.load > incumbent.load * (1.0 + self._policy.load_tie_tolerance):
            return f"higher current load overrides incumbent; {factors}"
        if selected.last_backwash != incumbent.last_backwash:
            return f"loads close; older last backwash preferred; {factors}"
        if selected.cycle_production != incumbent.cycle_production:
            return f"loads close; lower cycle production preferred; {factors}"
        return f"loads close; stable duty selection; {factors}"
