"""Deterministic four-zone lighting effect frame generation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from colorsys import hsv_to_rgb
from math import cos, isfinite, pi, sin

from .models import ColorLayout, FIRMWARE_ZONE_ORDER, Zone


class EffectKind(str, Enum):
    STATIC = "Static"
    BLINK = "Blink"
    BREATHE = "Breathe"
    PULSE = "Pulse"
    STROBE = "Strobe"
    CYCLE = "Cycle"
    WAVE = "Wave"
    GRADIENT = "Gradient"
    REACTIVE = "Reactive"
    RIPPLE = "Ripple"
    RAIN = "Rain"
    FIRE = "Fire"
    AURORA = "Aurora"
    SPARKLE = "Sparkle"
    COMET = "Comet"
    SCANNER = "Scanner"


@dataclass(frozen=True)
class EffectEvent:
    zone: Zone
    elapsed: float

    def __post_init__(self) -> None:
        if not isinstance(self.zone, Zone):
            raise ValueError("event zone must be a Zone")
        if not isfinite(self.elapsed) or self.elapsed < 0.0:
            raise ValueError("event elapsed time must be finite and non-negative")


@dataclass(frozen=True)
class EffectSpec:
    kind: EffectKind
    base: ColorLayout
    speed: float = 0.5
    light: float = 1.0
    direction: int = 1
    seed: int = 0
    cycle_from_deg: int = 0
    cycle_to_deg: int = 360
    cycle_saturation: float = 0.9

    def __post_init__(self) -> None:
        if not isinstance(self.kind, EffectKind):
            raise TypeError("kind must be an EffectKind")
        if not isinstance(self.base, ColorLayout):
            raise TypeError("base must be a ColorLayout")
        if not isfinite(self.speed) or not 0.0 < self.speed <= 1.0:
            raise ValueError("speed must be finite and within (0, 1]")
        if not isfinite(self.light) or not 0.0 <= self.light <= 1.0:
            raise ValueError("light must be finite and within [0, 1]")
        if self.direction not in (-1, 1):
            raise ValueError("direction must be -1 or 1")
        if type(self.seed) is not int:
            raise TypeError("seed must be an integer")
        if (
            type(self.cycle_from_deg) is not int
            or type(self.cycle_to_deg) is not int
            or not 0 <= self.cycle_from_deg < self.cycle_to_deg <= 360
        ):
            raise ValueError("Cycle hues must satisfy 0 <= from < to <= 360")
        if (
            type(self.cycle_saturation) not in (float, int)
            or not isfinite(self.cycle_saturation)
            or not 0.0 <= self.cycle_saturation <= 1.0
        ):
            raise ValueError("Cycle saturation must be finite and within [0, 1]")


def _scale_color(color: str, factor: float) -> str:
    factor = max(0.0, min(1.0, factor))
    return "".join(
        f"{round(int(color[offset : offset + 2], 16) * factor):02X}"
        for offset in (0, 2, 4)
    )


def _scale_layout(layout: ColorLayout, factor: float) -> ColorLayout:
    values = (
        _scale_color(getattr(layout, zone.value), factor)
        for zone in FIRMWARE_ZONE_ORDER
    )
    return ColorLayout.from_wire(",".join(values))


PHYSICAL_ZONE_ORDER = (Zone.LEFT, Zone.WASD, Zone.CENTER, Zone.RIGHT)


def _rgb(color: str) -> tuple[int, int, int]:
    return tuple(int(color[offset : offset + 2], 16) for offset in (0, 2, 4))


def _hex(channels: tuple[float, float, float]) -> str:
    return "".join(f"{max(0, min(255, round(channel))):02X}" for channel in channels)


def _mix(first: str, second: str, amount: float) -> str:
    amount = max(0.0, min(1.0, amount))
    return _hex(
        tuple(
            left + (right - left) * amount
            for left, right in zip(_rgb(first), _rgb(second))
        )
    )


def _hsv(hue: float, saturation: float, value: float) -> str:
    red, green, blue = hsv_to_rgb(hue % 1.0, saturation, max(0.0, min(1.0, value)))
    return _hex((red * 255.0, green * 255.0, blue * 255.0))


def _layout_from_named(colors: dict[Zone, str]) -> ColorLayout:
    return ColorLayout(
        right=colors[Zone.RIGHT],
        center=colors[Zone.CENTER],
        left=colors[Zone.LEFT],
        wasd=colors[Zone.WASD],
    )


def _zone_order(direction: int) -> tuple[Zone, ...]:
    return PHYSICAL_ZONE_ORDER if direction == 1 else tuple(reversed(PHYSICAL_ZONE_ORDER))


def _noise(seed: int, step: int, index: int) -> float:
    value = (
        (seed * 0x9E3779B1)
        + (step * 0x85EBCA77)
        + (index * 0xC2B2AE3D)
    ) & 0xFFFFFFFF
    value ^= value >> 16
    value = (value * 0x7FEB352D) & 0xFFFFFFFF
    value ^= value >> 15
    value = (value * 0x846CA68B) & 0xFFFFFFFF
    value ^= value >> 16
    return value / 0xFFFFFFFF


def _period_phase(elapsed: float, period: float) -> float:
    return (elapsed % period) / period


def frame_at(
    spec: EffectSpec,
    elapsed: float,
    *,
    event: EffectEvent | None = None,
) -> ColorLayout:
    """Return one complete deterministic layout for a monotonic elapsed time."""
    if not isfinite(elapsed) or elapsed < 0.0:
        raise ValueError("elapsed must be finite and non-negative")
    if spec.kind is EffectKind.STATIC:
        return spec.base
    if spec.kind is EffectKind.BLINK:
        period = 1.0 / spec.speed
        bright = (elapsed % period) < period / 2.0
        factor = spec.light if bright else max(0.08, spec.light * 0.08)
        return _scale_layout(spec.base, factor)
    if spec.kind is EffectKind.BREATHE:
        period = 2.0 / spec.speed
        phase = (elapsed % period) / period
        wave = (1.0 - cos(2.0 * pi * phase)) / 2.0
        floor = min(spec.light, 0.08)
        return _scale_layout(spec.base, floor + (spec.light - floor) * wave)
    if spec.kind is EffectKind.PULSE:
        phase = _period_phase(elapsed, 1.5 / spec.speed)
        wave = max(0.0, sin(pi * phase)) ** 4
        floor = min(spec.light, 0.08)
        return _scale_layout(spec.base, floor + (spec.light - floor) * wave)
    if spec.kind is EffectKind.STROBE:
        phase = _period_phase(elapsed, 0.35 / spec.speed)
        factor = spec.light if phase < 0.18 else max(0.05, spec.light * 0.05)
        return _scale_layout(spec.base, factor)
    if spec.kind is EffectKind.CYCLE:
        phase = _period_phase(elapsed, 4.0 / spec.speed)
        if spec.direction == -1:
            phase = (-phase) % 1.0
        colors = {
            zone: _hsv(
                (spec.cycle_from_deg + (spec.cycle_to_deg - spec.cycle_from_deg)
                 * ((phase + index / 12.0) % 1.0)) / 360.0,
                spec.cycle_saturation,
                spec.light,
            )
            for index, zone in enumerate(PHYSICAL_ZONE_ORDER)
        }
        return _layout_from_named(colors)
    if spec.kind is EffectKind.WAVE:
        phase = _period_phase(elapsed, 2.5 / spec.speed)
        colors = {}
        for index, zone in enumerate(_zone_order(spec.direction)):
            wave = (1.0 + cos(2.0 * pi * (phase - index / 4.0))) / 2.0
            factor = 0.12 + (spec.light - 0.12) * wave
            colors[zone] = _scale_color(getattr(spec.base, zone.value), factor)
        return _layout_from_named(colors)
    if spec.kind is EffectKind.GRADIENT:
        phase = _period_phase(elapsed, 5.0 / spec.speed)
        sources = [getattr(spec.base, zone.value) for zone in PHYSICAL_ZONE_ORDER]
        colors = {}
        for index, zone in enumerate(PHYSICAL_ZONE_ORDER):
            position = (index + phase * 4.0) % 4.0
            left_index = int(position) % 4
            right_index = (left_index + 1) % 4
            colors[zone] = _scale_color(
                _mix(sources[left_index], sources[right_index], position % 1.0),
                spec.light,
            )
        return _layout_from_named(colors)
    if spec.kind is EffectKind.REACTIVE:
        if event is None:
            return spec.base
        age = elapsed - event.elapsed
        if age < 0.0 or age >= 0.8:
            return spec.base
        floor = min(spec.light, 0.12)
        strength = 1.0 - age / 0.8
        colors = {}
        for zone in FIRMWARE_ZONE_ORDER:
            factor = floor
            if zone is event.zone:
                factor = floor + (spec.light - floor) * strength
            colors[zone] = _scale_color(getattr(spec.base, zone.value), factor)
        return _layout_from_named(colors)
    if spec.kind is EffectKind.RIPPLE:
        if event is None:
            return spec.base
        age = elapsed - event.elapsed
        if age < 0.0:
            return spec.base
        positions = {
            Zone.LEFT: 0.0,
            Zone.WASD: 0.0,
            Zone.CENTER: 1.0,
            Zone.RIGHT: 2.0,
        }
        delay_step = 0.22 / spec.speed
        pulse_width = 0.18 / spec.speed
        if age >= 2.0 * delay_step + pulse_width:
            return spec.base
        origin = positions[event.zone]
        floor = min(spec.light, 0.10)
        colors = {}
        for zone in FIRMWARE_ZONE_ORDER:
            delay = abs(positions[zone] - origin) * delay_step
            pulse = max(0.0, 1.0 - abs(age - delay) / pulse_width)
            if zone is Zone.WASD and event.zone is not Zone.WASD:
                pulse *= 0.75
            factor = floor + (spec.light - floor) * pulse
            colors[zone] = _scale_color(getattr(spec.base, zone.value), factor)
        return _layout_from_named(colors)
    if spec.kind is EffectKind.RAIN:
        step = int(elapsed * (2.0 + spec.speed * 6.0))
        colors = {}
        for index, zone in enumerate(PHYSICAL_ZONE_ORDER):
            noise = _noise(spec.seed, step, index)
            hue = 0.52 + 0.18 * _noise(spec.seed + 11, step, index)
            value = spec.light * (0.18 + 0.82 * noise)
            colors[zone] = _hsv(hue, 0.75, value)
        return _layout_from_named(colors)
    if spec.kind is EffectKind.FIRE:
        step = int(elapsed * (4.0 + spec.speed * 8.0))
        colors = {}
        for index, zone in enumerate(PHYSICAL_ZONE_ORDER):
            noise = _noise(spec.seed, step, index)
            hue = 0.015 + 0.11 * noise
            value = spec.light * (0.55 + 0.45 * _noise(spec.seed + 23, step, index))
            colors[zone] = _hsv(hue, 0.95, value)
        return _layout_from_named(colors)
    if spec.kind is EffectKind.AURORA:
        phase = _period_phase(elapsed, 6.0 / spec.speed)
        palette = ("20E3B2", "19D8FF", "7B2CFF", "FF2E88")
        colors = {}
        for index, zone in enumerate(PHYSICAL_ZONE_ORDER):
            position = (phase * 4.0 + index * 0.65) % 4.0
            left_index = int(position) % 4
            colors[zone] = _scale_color(
                _mix(
                    palette[left_index],
                    palette[(left_index + 1) % 4],
                    position % 1.0,
                ),
                spec.light,
            )
        return _layout_from_named(colors)
    if spec.kind is EffectKind.SPARKLE:
        step = int(elapsed * (2.0 + spec.speed * 6.0))
        selected_index = (step + spec.seed) % 4
        colors = {}
        for index, zone in enumerate(PHYSICAL_ZONE_ORDER):
            if index == selected_index:
                hue = _noise(spec.seed + 31, step, index)
                colors[zone] = _hsv(hue, 0.35, spec.light)
            else:
                noise = _noise(spec.seed, step, index)
                colors[zone] = _scale_color(
                    getattr(spec.base, zone.value),
                    0.08 + noise * 0.12,
                )
        return _layout_from_named(colors)
    if spec.kind is EffectKind.COMET:
        order = _zone_order(spec.direction)
        phase = _period_phase(elapsed, 2.5 / spec.speed) * 4.0
        colors = {}
        for index, zone in enumerate(order):
            distance = (phase - index) % 4.0
            if distance < 1.0:
                factor = spec.light * (1.0 - distance * 0.35)
            elif distance < 2.0:
                factor = spec.light * 0.35 * (2.0 - distance)
            else:
                factor = 0.06
            colors[zone] = _scale_color(getattr(spec.base, zone.value), factor)
        return _layout_from_named(colors)
    if spec.kind is EffectKind.SCANNER:
        order = _zone_order(spec.direction)
        travel = _period_phase(elapsed, 3.0 / spec.speed) * 6.0
        position = travel if travel <= 3.0 else 6.0 - travel
        colors = {}
        for index, zone in enumerate(order):
            distance = abs(position - index)
            factor = max(0.07, spec.light * max(0.0, 1.0 - distance * 0.65))
            colors[zone] = _scale_color(getattr(spec.base, zone.value), factor)
        return _layout_from_named(colors)
    raise AssertionError(f"unhandled effect kind: {spec.kind.value}")
