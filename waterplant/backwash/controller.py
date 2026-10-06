"""Backwash controller.

Every backwash follows the same sequence: the bed is closed and taken off duty
first, then the drain is opened for it. Finishing a wash opens the bed, records
the completion time used by duty rotation, and starts a new production cycle.
"""

from __future__ import annotations

import json
from typing import Callable

from waterplant.filter import Bank, RotationService
from waterplant.store.commands import append_command, clear_commands, load_commands
from waterplant.store.store import Store

from .report import BackwashState
from .schedule import now_unix

COMMAND_KEY = "backwash:commands"
DRAIN_KEY_PREFIX = "backwash:drain:"
SPILL_KEY = "backwash:spill"


class Controller:
    """Sequences backwash commands against the filter bank."""

    def __init__(
        self,
        bank: Bank,
        store: Store,
        rotation: RotationService | None = None,
        auditor: object | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._bank = bank
        self._store = store
        self._rotation = rotation
        self._auditor = auditor
        self._clock = clock or now_unix

    def set_rotation(self, rotation: RotationService) -> None:
        self._rotation = rotation

    def start(self, bed_id: str) -> None:
        """Take the bed off duty, close it, and only then open its drain."""

        if self._bank.bed(bed_id) is None:
            raise ValueError(f"filter bed {bed_id} not found")
        self._write_state(bed_id, started_at=self._clock(), finished_at=0, active=True)
        self._bank.begin_backwash(bed_id)

    def finish(self, bed_id: str) -> None:
        """Complete the wash and reset the bed's per-cycle production total."""

        state = self._read_state(bed_id)
        if state is None:
            raise ValueError(f"filter bed {bed_id} is not backwashing")
        completed_at = self._clock()
        self._write_state(
            bed_id,
            started_at=int(state.get("started_at", 0)),
            finished_at=completed_at,
            active=False,
        )
        self._bank.complete_backwash(bed_id, completed_at)

    def enqueue(self, bed_id: str) -> None:
        append_command(self._store, COMMAND_KEY, bed_id)

    def replay(self) -> list[str]:
        """Re-run every queued command in the order it was written."""

        commands = self.command_list()
        for bed_id in commands:
            self.start(bed_id)
        return commands

    def recover(self) -> None:
        """Discard queued commands and reconcile every open drain as complete."""

        for bed_id in self.active():
            self.finish(bed_id)
        clear_commands(self._store, COMMAND_KEY)

    def command_list(self) -> list[str]:
        return load_commands(self._store, COMMAND_KEY)

    def pending_count(self) -> int:
        return len(self.command_list())

    def select(self, zone: int) -> str:
        """Resolve the bed that currently occupies a zone."""

        mapping = self._bank.mapping()
        if zone not in mapping:
            raise ValueError(f"no filter bed for zone {zone}")
        return mapping[zone]

    def order_rotation(self) -> list[str]:
        """Rotate duty through eligible beds using the fair rotation policy."""

        if self._rotation is not None:
            result = self._rotation.rotate()
        else:
            auditor = self._auditor if self._auditor is not None else _NullAuditor()
            result = RotationService(
                self._bank, self, auditor, clock=self._clock
            ).rotate()
        return result.order

    def eligible(self) -> list[str]:
        return [
            bed.id
            for bed in self._bank.beds()
            if not bed.closed and bed.id not in self.active()
        ]

    def drains(self) -> list[str]:
        """All beds with a recorded drain, including completed washes."""

        drains: list[str] = []
        for key in self._store.keys():
            if key.startswith(DRAIN_KEY_PREFIX):
                bed_id = key[len(DRAIN_KEY_PREFIX) :]
                if self._read_state(bed_id) is not None:
                    drains.append(bed_id)
        return drains

    def active(self) -> list[str]:
        return [bed_id for bed_id in self.drains() if self._is_active(bed_id)]

    def last_finish(self, bed_id: str) -> int:
        state = self._read_state(bed_id)
        if state is None:
            return 0
        return int(state.get("finished_at", 0))

    def drain_count(self) -> int:
        return len(self.drains())

    def last_drain(self) -> str | None:
        drains = self.drains()
        if not drains:
            return None
        return drains[-1]

    def spilled(self) -> bool:
        return self._store.get(SPILL_KEY)[1]

    def state(self) -> BackwashState:
        drains = self.drains()
        return BackwashState(
            commands=self.command_list(),
            drains=drains,
            active=self.active(),
            history=[bed_id for bed_id in drains if not self._is_active(bed_id)],
        )

    def describe(self) -> str:
        state = self.state()
        return (
            f"backwash commands={len(state.commands)} "
            f"drains={len(state.drains)} active={len(state.active)}"
        )

    def _write_state(
        self, bed_id: str, started_at: int, finished_at: int, active: bool
    ) -> None:
        state = {
            "bed_id": bed_id,
            "started_at": started_at,
            "finished_at": finished_at,
            "active": active,
        }
        self._store.put(
            f"{DRAIN_KEY_PREFIX}{bed_id}",
            json.dumps(state, ensure_ascii=False),
        )

    def _read_state(self, bed_id: str) -> dict[str, object] | None:
        raw, present = self._store.get(f"{DRAIN_KEY_PREFIX}{bed_id}")
        if not present:
            return None
        try:
            state = json.loads(raw)
        except ValueError as exc:
            raise ValueError(f"backwash state unavailable for bed {bed_id}") from exc
        if not isinstance(state, dict):
            raise ValueError(f"backwash state unavailable for bed {bed_id}")
        return state

    def _is_active(self, bed_id: str) -> bool:
        state = self._read_state(bed_id)
        return bool(state and state.get("active", False))


class _NullAuditor:
    def record(self, kind: str, detail: str) -> None:
        return None
