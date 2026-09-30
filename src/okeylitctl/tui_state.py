"""Pure, testable state transitions for the interactive terminal UI."""

from __future__ import annotations

from dataclasses import dataclass, field

from .effects import EffectKind, EffectSpec
from .models import ColorLayout, FIRMWARE_ZONE_ORDER, Zone
from .validation import normalize_color


@dataclass
class TuiState:
    """Local draft state; firmware changes occur only after explicit Apply."""

    current: ColorLayout
    original: ColorLayout
    draft: ColorLayout = field(init=False)
    selected_zone: Zone = Zone.RIGHT
    selected_channel: int = 0
    effect_index: int = 0
    effect_speed: float = 0.6
    effect_light: float = 0.8
    effect_direction: int = 1
    effect_running: bool = False
    preset_index: int = -1
    help_visible: bool = False
    message: str = "Ready"

    def __post_init__(self) -> None:
        self.draft = self.current

    @property
    def dirty(self) -> bool:
        return self.draft != self.current

    @property
    def selected_color(self) -> str:
        return getattr(self.draft, self.selected_zone.value)

    @property
    def effect_kind(self) -> EffectKind:
        return tuple(EffectKind)[self.effect_index]

    @property
    def effect_spec(self) -> EffectSpec:
        return EffectSpec(
            kind=self.effect_kind,
            base=self.draft,
            speed=self.effect_speed,
            light=self.effect_light,
            direction=self.effect_direction,
        )

    def select_next_effect(self) -> None:
        self.effect_index = (self.effect_index + 1) % len(EffectKind)

    def select_previous_effect(self) -> None:
        self.effect_index = (self.effect_index - 1) % len(EffectKind)

    def adjust_effect_speed(self, delta: float) -> None:
        self.effect_speed = round(max(0.1, min(1.0, self.effect_speed + delta)), 2)

    def adjust_effect_light(self, delta: float) -> None:
        self.effect_light = round(max(0.1, min(1.0, self.effect_light + delta)), 2)

    def toggle_effect_direction(self) -> None:
        self.effect_direction *= -1

    def select_next_zone(self) -> None:
        index = FIRMWARE_ZONE_ORDER.index(self.selected_zone)
        self.selected_zone = FIRMWARE_ZONE_ORDER[(index + 1) % len(FIRMWARE_ZONE_ORDER)]

    def select_previous_zone(self) -> None:
        index = FIRMWARE_ZONE_ORDER.index(self.selected_zone)
        self.selected_zone = FIRMWARE_ZONE_ORDER[(index - 1) % len(FIRMWARE_ZONE_ORDER)]

    def set_selected_color(self, color: str) -> None:
        self.draft = self.draft.with_zone(self.selected_zone, normalize_color(color))
        self.preset_index = -1

    def adjust_selected_channel(self, delta: int) -> None:
        if self.selected_channel not in (0, 1, 2):
            raise ValueError("selected channel must be 0, 1, or 2")
        channels = [
            int(self.selected_color[offset : offset + 2], 16)
            for offset in (0, 2, 4)
        ]
        channels[self.selected_channel] = max(
            0, min(255, channels[self.selected_channel] + delta)
        )
        self.set_selected_color("".join(f"{channel:02X}" for channel in channels))

    def mark_applied(self) -> None:
        self.current = self.draft
        self.message = "Layout applied and verified"

    def mark_restored(self) -> None:
        self.current = self.original
        self.draft = self.original
        self.preset_index = -1
        self.message = "Original layout restored and verified"

    def discard_draft(self) -> None:
        self.draft = self.current
        self.preset_index = -1
        self.message = "Unapplied changes discarded"
