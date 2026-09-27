"""Typed lighting models with one explicit firmware-zone order."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from .validation import normalize_color, normalize_colors


class Zone(str, Enum):
    """Named physical zones; values also name ColorLayout fields."""

    RIGHT = "right"
    CENTER = "center"
    LEFT = "left"
    WASD = "wasd"


FIRMWARE_ZONE_ORDER = (Zone.RIGHT, Zone.CENTER, Zone.LEFT, Zone.WASD)


@dataclass(frozen=True)
class ColorLayout:
    """A complete four-zone layout, independent of UI widget ordering."""

    right: str
    center: str
    left: str
    wasd: str

    def __post_init__(self) -> None:
        for zone in FIRMWARE_ZONE_ORDER:
            object.__setattr__(self, zone.value, normalize_color(getattr(self, zone.value)))

    @classmethod
    def from_wire(cls, value: str) -> "ColorLayout":
        right, center, left, wasd = normalize_colors(value).split(",")
        return cls(right=right, center=center, left=left, wasd=wasd)

    def to_wire(self) -> str:
        return ",".join(getattr(self, zone.value) for zone in FIRMWARE_ZONE_ORDER)

    def with_zone(self, zone: Zone, color: str) -> "ColorLayout":
        if not isinstance(zone, Zone):
            raise TypeError("zone must be a Zone")
        return replace(self, **{zone.value: normalize_color(color)})
