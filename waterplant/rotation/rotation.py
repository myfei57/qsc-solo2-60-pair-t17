"""Duty rotation for the filter bank.

The old behaviour picked the dirtiest bed by current load alone, so beds with
similar loads swapped duty back and forth while the rest never served. This
component makes the rotation deterministic and auditable.

Fixed operating policy (decided once, not configurable):

1.  Cumulative production is tracked per backwash cycle, never per shift.
    A bed accumulates produced volume from the moment its last backwash was
    recorded and the counter resets to zero at the next backwash. The reset
    is anchored to the backwash drain record, so duty bookkeeping and wash
    records can never disagree about which cycle a counter belongs to.

2.  When bed loads are within the configured tolerance the tie is broken by
    lowest cumulative production first, then by the oldest last-backwash
    time (a bed never washed counts as the oldest), then by bed id.

Safety and failure rules:

*   A bed in backwash (closed) is never rotated on duty. Backwashing the
    duty bed forces an immediate handoff first: rotation always yields to
    the wash, never the other way around.
*   If any bed status cannot be read the evaluation is a partial failure:
    the current duty bed stays exactly as it is and nothing is rewritten.
*   Repeating a trigger with unchanged state is a no-op: duty records and
    audit entries are written only when duty actually changes.
*   Every change of duty is audited with its reason and kept in the last
    decision record so the console can show why the rotation moved.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from waterplant.audit import Auditor
from waterplant.backwash.controller import DRAIN_KEY_PREFIX
from waterplant.backwash.schedule import now_unix
from waterplant.filter import Bank
from waterplant.store.epoch import load_float, save_float
from waterplant.store.store import Store

from .report import (
    CandidateState,
    Decision,
    RotationState,
    validate_min_dwell,
    validate_production,
    validate_tolerance,
)

DUTY_KEY = "rotation:duty"
DUTY_SINCE_KEY = "rotation:duty-since"
DECISION_KEY = "rotation:decision"
MIN_DWELL_KEY = "rotation:min-dwell-seconds"
TOLERANCE_KEY = "rotation:load-tolerance"
PRODUCED_KEY_PREFIX = "rotation:produced:"

DEFAULT_MIN_DWELL_SECONDS = 300.0
DEFAULT_LOAD_TOLERANCE = 0.5
AUDIT_KIND = "rotation"


@dataclass(frozen=True)
class _Profile:
    """Everything the evaluator needs to know about one bed."""

    id: str
    load: float
    closed: bool
    production: float
    last_wash: int


class Rotation:
    """Evaluates which bed should hold duty and applies the decision."""

    def __init__(self, bank: Bank, store: Store, auditor: Auditor) -> None:
        self._bank = bank
        self._store = store
        self._auditor = auditor

    def min_dwell_seconds(self) -> float:
        value, present = load_float(self._store, MIN_DWELL_KEY)
        return value if present else DEFAULT_MIN_DWELL_SECONDS

    def set_min_dwell_seconds(self, seconds: float) -> float:
        validate_min_dwell(seconds)
        save_float(self._store, MIN_DWELL_KEY, seconds)
        return seconds

    def load_tolerance(self) -> float:
        value, present = load_float(self._store, TOLERANCE_KEY)
        return value if present else DEFAULT_LOAD_TOLERANCE

    def set_load_tolerance(self, value: float) -> float:
        validate_tolerance(value)
        save_float(self._store, TOLERANCE_KEY, value)
        return value

    def production(self, bed_id: str) -> float:
        """Effective per-cycle production of one bed, zero when unreadable."""

        amount, _, ok = self._readings(bed_id)
        return amount if ok else 0.0

    def record_production(self, bed_id: str, amount: float) -> float:
        """Credit produced volume to a bed inside its current wash cycle."""

        validate_production(amount)
        if self._bank.bed(bed_id) is None:
            raise ValueError(f"filter bed {bed_id} not found")
        wash, ok = self._last_wash(bed_id)
        if not ok:
            raise ValueError(f"wash record for bed {bed_id} is unreadable")
        stored, anchor, ok = self._stored_record(bed_id)
        if not ok:
            raise ValueError(f"production record for bed {bed_id} is unreadable")
        base = stored if anchor == wash else 0.0
        total = base + amount
        self._store.put(
            f"{PRODUCED_KEY_PREFIX}{bed_id}",
            json.dumps({"amount": total, "anchor": wash}, ensure_ascii=False),
        )
        return total

    def reset_production(self, bed_id: str | None = None) -> None:
        """Clear production counters, one bed or all; safe recovery path."""

        if bed_id is not None:
            self._store.delete(f"{PRODUCED_KEY_PREFIX}{bed_id}")
            return
        for key in self._store.keys():
            if key.startswith(PRODUCED_KEY_PREFIX):
                self._store.delete(key)

    def evaluate(self, trigger: str = "manual") -> Decision:
        """Run one rotation pass; only a real duty change writes records."""

        now = now_unix()
        profiles, error = self._profiles()
        if error:
            decision = Decision(
                action="blocked",
                duty=self._bank.on_duty() or "",
                changed=False,
                reason=error,
                trigger=trigger,
                ranked=[],
                at=now,
            )
            self._save_decision(decision)
            return decision

        eligible = [profile for profile in profiles if not profile.closed]
        ranked = [profile.id for profile in self._rank(eligible)]
        current = self._bank.on_duty()

        if current is not None and current not in {profile.id for profile in eligible}:
            # The duty bed left service (backwash or removal): the interlock
            # outranks the dwell window, duty must move now.
            if eligible:
                best, why = self._pick(eligible)
                return self._apply(
                    "switch", best.id, f"duty bed {current} unavailable; {why}", trigger, ranked, now
                )
            return self._apply(
                "release",
                "",
                f"duty bed {current} unavailable; no eligible bed",
                trigger,
                ranked,
                now,
            )

        if not eligible:
            decision = Decision(
                action="hold",
                duty=current or "",
                changed=False,
                reason="no eligible bed",
                trigger=trigger,
                ranked=ranked,
                at=now,
            )
            self._save_decision(decision)
            return decision

        best, why = self._pick(eligible)
        if current is None:
            return self._apply("assign", best.id, why, trigger, ranked, now)
        if best.id == current:
            decision = Decision(
                action="hold",
                duty=current,
                changed=False,
                reason=f"incumbent remains best: {why}",
                trigger=trigger,
                ranked=ranked,
                at=now,
            )
            self._save_decision(decision)
            return decision

        age = now - self._duty_since()
        dwell = self.min_dwell_seconds()
        if age < dwell:
            decision = Decision(
                action="hold",
                duty=current,
                changed=False,
                reason=f"dwell {age}s < {dwell:.0f}s; best={best.id} ({why})",
                trigger=trigger,
                ranked=ranked,
                at=now,
            )
            self._save_decision(decision)
            return decision
        return self._apply("switch", best.id, why, trigger, ranked, now)

    def release_if_duty(self, bed_id: str, reason: str = "backwash") -> Decision | None:
        """Move duty off a bed that is about to leave service.

        Backwash depends on rotation, not the reverse: the wash always
        proceeds, so duty hands off first. Returns ``None`` when the bed does
        not hold duty, which keeps repeated calls harmless.
        """

        if self._bank.on_duty() != bed_id:
            return None
        now = now_unix()
        profiles, error = self._profiles()
        if error:
            # A replacement cannot be ranked, but the interlock still wins
            # over the hold: a bed entering backwash must not keep duty.
            return self._apply(
                "release", "", f"{reason}; replacement evaluation blocked: {error}", "backwash", [], now
            )
        eligible = [
            profile for profile in profiles if not profile.closed and profile.id != bed_id
        ]
        if not eligible:
            return self._apply("release", "", f"{reason}; no eligible bed", "backwash", [], now)
        best, why = self._pick(eligible)
        ranked = [profile.id for profile in self._rank(eligible)]
        return self._apply("handoff", best.id, f"{reason}; {why}", "backwash", ranked, now)

    def ranked(self) -> list[str]:
        """Eligible beds in rotation preference order, winner first."""

        profiles, error = self._profiles()
        if error:
            return []
        eligible = [profile for profile in profiles if not profile.closed]
        return [profile.id for profile in self._rank(eligible)]

    def state(self) -> RotationState:
        candidates: list[CandidateState] = []
        for bed in self._bank.beds():
            production, wash, ok = self._readings(bed.id)
            candidates.append(
                CandidateState(
                    id=bed.id,
                    load=bed.load,
                    production=production,
                    last_wash=wash,
                    eligible=not bed.closed,
                    readable=ok,
                )
            )
        return RotationState(
            duty=self._bank.on_duty() or "",
            duty_since=self._duty_since(),
            min_dwell_seconds=self.min_dwell_seconds(),
            load_tolerance=self.load_tolerance(),
            ranked=self.ranked(),
            candidates=candidates,
            last_decision=self._load_decision(),
        )

    def describe(self) -> str:
        state = self.state()
        eligible = sum(1 for candidate in state.candidates if candidate.eligible)
        summary = f"rotation duty={state.duty or ''} eligible={eligible}/{len(state.candidates)}"
        if state.last_decision is not None:
            summary += f" last={state.last_decision.action}"
        return summary

    def _apply(
        self,
        action: str,
        new_duty: str,
        reason: str,
        trigger: str,
        ranked: list[str],
        now: int,
    ) -> Decision:
        """Move duty, then persist the duty record and audit the reason."""

        previous = self._bank.on_duty()
        if new_duty:
            self._bank.rotate(new_duty)
            self._store.put(DUTY_KEY, new_duty)
            self._store.put(DUTY_SINCE_KEY, str(now))
        else:
            self._bank.clear_duty()
            self._store.delete(DUTY_KEY)
            self._store.delete(DUTY_SINCE_KEY)
        self._auditor.record(
            AUDIT_KIND,
            f"{action} duty={new_duty or '-'} previous={previous or '-'} reason={reason}",
        )
        decision = Decision(
            action=action,
            duty=new_duty,
            changed=True,
            reason=reason,
            trigger=trigger,
            ranked=ranked,
            at=now,
        )
        self._save_decision(decision)
        return decision

    def _pick(self, eligible: list[_Profile]) -> tuple[_Profile, str]:
        """Choose one bed and explain which factor decided it."""

        tolerance = self.load_tolerance()
        top = max(profile.load for profile in eligible)
        group = [profile for profile in eligible if top - profile.load <= tolerance]
        if len(group) == 1:
            return group[0], f"highest load {top:.4f}"
        winner = min(group, key=lambda profile: (profile.production, profile.last_wash, profile.id))
        if len({profile.production for profile in group}) > 1:
            factor = f"least production {winner.production:.4f}"
        elif len({profile.last_wash for profile in group}) > 1:
            factor = f"oldest wash {self._wash_label(winner.last_wash)}"
        else:
            factor = "id order"
        return winner, f"load tie within {tolerance:.4f}; {factor}"

    def _rank(self, eligible: list[_Profile]) -> list[_Profile]:
        """Order the eligible beds by repeatedly picking the next winner."""

        remaining = list(eligible)
        ordered: list[_Profile] = []
        while remaining:
            best, _ = self._pick(remaining)
            ordered.append(best)
            remaining = [profile for profile in remaining if profile.id != best.id]
        return ordered

    def _profiles(self) -> tuple[list[_Profile], str]:
        """Read every bed; any unreadable status fails the whole pass."""

        profiles: list[_Profile] = []
        for bed in self._bank.beds():
            production, wash, ok = self._readings(bed.id)
            if not ok:
                return [], f"bed {bed.id} status unreadable"
            profiles.append(
                _Profile(
                    id=bed.id,
                    load=bed.load,
                    closed=bed.closed,
                    production=production,
                    last_wash=wash,
                )
            )
        return profiles, ""

    def _readings(self, bed_id: str) -> tuple[float, int, bool]:
        """Effective production and last wash time for one bed."""

        wash, ok = self._last_wash(bed_id)
        if not ok:
            return 0.0, 0, False
        amount, anchor, ok = self._stored_record(bed_id)
        if not ok:
            return 0.0, 0, False
        # A newer wash record starts a fresh cycle: the counter resets.
        return (amount if anchor == wash else 0.0), wash, True

    def _last_wash(self, bed_id: str) -> tuple[int, bool]:
        """Last backwash time from the drain record; (0, True) if never washed."""

        raw, present = self._store.get(f"{DRAIN_KEY_PREFIX}{bed_id}")
        if not present:
            return 0, True
        try:
            return int(raw), True
        except ValueError:
            return 0, False

    def _stored_record(self, bed_id: str) -> tuple[float, int, bool]:
        """Raw production record: (amount, anchor wash time, readable)."""

        raw, present = self._store.get(f"{PRODUCED_KEY_PREFIX}{bed_id}")
        if not present:
            return 0.0, -1, True
        try:
            payload = json.loads(raw)
        except ValueError:
            return 0.0, -1, False
        if not isinstance(payload, dict):
            return 0.0, -1, False
        try:
            return float(payload["amount"]), int(payload["anchor"]), True
        except (KeyError, TypeError, ValueError):
            return 0.0, -1, False

    def _duty_since(self) -> int:
        raw, present = self._store.get(DUTY_SINCE_KEY)
        if not present:
            return 0
        try:
            return int(raw)
        except ValueError:
            return 0

    def _save_decision(self, decision: Decision) -> None:
        self._store.put(DECISION_KEY, json.dumps(decision.as_dict(), ensure_ascii=False))

    def _load_decision(self) -> Decision | None:
        raw, present = self._store.get(DECISION_KEY)
        if not present:
            return None
        try:
            payload = json.loads(raw)
        except ValueError:
            return None
        return Decision.from_dict(payload)

    @staticmethod
    def _wash_label(last_wash: int) -> str:
        return "never" if last_wash <= 0 else str(last_wash)
