"""A single filter bed."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Bed:
    """Mutable state of one filter bed."""

    id: str
    zone: int
    load: float = 0.0
    closed: bool = False
    duty: bool = False
    backwashing: bool = False
    cycle_production: float = 0.0
    last_backwash: int = 0
    last_duty: int = 0

    def snapshot(self) -> dict[str, object]:
        return {
            "id": self.id,
            "zone": self.zone,
            "load": self.load,
            "closed": self.closed,
            "duty": self.duty,
            "backwashing": self.backwashing,
            "cycle_production": self.cycle_production,
            "last_backwash": self.last_backwash,
            "last_duty": self.last_duty,
        }
