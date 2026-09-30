"""Immutable geometry and palette contract for the supplied OKeyLitCtl mockups.

The editor mockups were captured on a 160-column, 55-row canvas.  The Profiles
mockup uses the same width and a shorter 43-row canvas.  Renderers may provide a
safe compact fallback for smaller terminals, but these coordinates are the
pixel-for-cell acceptance target at the reference sizes.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class Rect:
    name: str
    x: int
    y: int
    width: int
    height: int

    @property
    def right(self) -> int:
        """Exclusive right edge; x identifies the left border cell."""
        return self.x + self.width

    @property
    def bottom(self) -> int:
        """Exclusive bottom edge; y identifies the top border cell."""
        return self.y + self.height

    def as_tuple(self) -> tuple[int, int, int, int]:
        return self.x, self.y, self.width, self.height

    def overlaps(self, other: "Rect") -> bool:
        return (
            self.x < other.right
            and other.x < self.right
            and self.y < other.bottom
            and other.y < self.bottom
        )


@dataclass(frozen=True)
class ScreenLayout:
    width: int
    height: int
    panels: Mapping[str, Rect]

    def panel(self, name: str) -> Rect:
        return self.panels[name]


def _panels(*rects: Rect) -> Mapping[str, Rect]:
    return MappingProxyType({rect.name: rect for rect in rects})


EDITOR_LAYOUT = ScreenLayout(
    width=160,
    height=55,
    panels=_panels(
        Rect("keyboard", 6, 4, 107, 21),
        Rect("zones", 116, 4, 40, 9),
        Rect("live_draft", 116, 14, 40, 11),
        Rect("effect", 6, 25, 150, 11),
        Rect("color", 6, 36, 107, 11),
        Rect("motion", 116, 36, 40, 11),
        Rect("footer", 5, 49, 151, 4),
    ),
)

HEX_MODAL = Rect("exact_hex", 43, 17, 74, 18)
OUTER_FRAME = Rect("outer_frame", 0, 0, 160, 55)
HEADER = Rect("header", 3, 1, 154, 2)

FOOTER_ROWS = (
    "zone | channel | adjust | effect | speed | hex | preset",
    "apply | discard | original | profiles | refresh | help | quit",
)

PROFILES_LAYOUT = ScreenLayout(
    width=160,
    height=43,
    panels=_panels(
        Rect("profiles", 6, 4, 43, 33),
        Rect("preview", 53, 4, 104, 33),
        Rect("footer", 5, 39, 152, 2),
    ),
)

EFFECT_NAMES = (
    "Static",
    "Blink",
    "Breathe",
    "Pulse",
    "Strobe",
    "Cycle",
    "Wave",
    "Gradient",
    "Reactive",
    "Ripple",
    "Rain",
    "Fire",
    "Aurora",
    "Sparkle",
    "Comet",
    "Scanner",
)

REFERENCE_STATES = MappingProxyType(
    {
        "cycle_synced": MappingProxyType(
            {
                "status": "IN SYNC",
                "device": "DEVICE ON",
                "effect": "Cycle",
                "effect_index": "6/16",
                "zone_values": "cycling",
                "live": "#19D8FF",
                "draft": "#19D8FF",
                "message": "No changes",
                "color_mode": "Spectrum",
                "speed": "45%",
                "light": "100%",
                "direction": "left → right",
            }
        ),
        "ripple_dirty": MappingProxyType(
            {
                "status": "UNSAVED DRAFT",
                "device": "DEVICE ON",
                "effect": "Ripple",
                "effect_index": "10/16",
                "selected_zone": "WASD",
                "live": "#E8F7FF",
                "draft": "#4DF0C8",
                "delta": "R-155 G-7 B-55",
                "color_mode": "WASD",
                "speed": "60%",
                "light": "80%",
            }
        ),
        "hex_invalid": MappingProxyType(
            {
                "title": "EXACT HEX · WASD",
                "input": "#FF9E3",
                "error": "needs 1 more digit",
                "live": "#E8F7FF",
                "new": "waiting…",
                "actions": "Enter accept | Esc cancel | ^U clear",
            }
        ),
        "profiles": MappingProxyType(
            {
                "selected": "Sunset",
                "effect": "Breathe",
                "status": "IN SYNC",
                "device": "DEVICE ON",
                "actions": "select | load into draft | save current as | rename | delete | back",
            }
        ),
    }
)

PALETTE = MappingProxyType(
    {
        "background": "#0B0E18",
        "panel": "#111522",
        "border": "#30364C",
        "text": "#D7DCEF",
        "muted": "#737A93",
        "focus": "#63DED3",
        "synced": "#51D88A",
        "dirty": "#F5C85B",
        "header_fill": "#121827",
        "selection_fill": "#63DED3",
        "profile_selection_fill": "#252C43",
        "shortcut_fill": "#30364C",
        "modal_fill": "#171C30",
        "overlay_scrim": "#070911",
        "disabled": "#4C5268",
        "error": "#F5C85B",
        "channel_red": "#E14B5A",
        "channel_green": "#39D353",
        "channel_blue": "#3B82F6",
        "bright_text": "#F5F7FF",
        "dark_text": "#0B0E18",
    }
)


def render_contract_snapshot(layout: ScreenLayout) -> str:
    """Render the immutable panel skeleton used by visual snapshot tests."""
    canvas = [[" " for _ in range(layout.width)] for _ in range(layout.height)]

    def put(y: int, x: int, text: str) -> None:
        for offset, character in enumerate(text):
            if 0 <= y < layout.height and 0 <= x + offset < layout.width:
                canvas[y][x + offset] = character

    def box(rect: Rect, title: str) -> None:
        left, top = rect.x, rect.y
        right, bottom = rect.right - 1, rect.bottom - 1
        canvas[top][left] = "+"
        canvas[top][right] = "+"
        canvas[bottom][left] = "+"
        canvas[bottom][right] = "+"
        for column in range(left + 1, right):
            canvas[top][column] = "-"
            canvas[bottom][column] = "-"
        for row in range(top + 1, bottom):
            canvas[row][left] = "|"
            canvas[row][right] = "|"
        put(top, left + 2, f" {title.upper()} ")

    frame = Rect("frame", 0, 0, layout.width, layout.height)
    box(frame, "frame")
    if layout is EDITOR_LAYOUT:
        put(1, 3, "okeylitctl v0.2.0")
        badges = "[ IN SYNC ]  [ DEVICE ON ]"
        put(1, layout.width - 3 - len(badges), badges)
        titles = {
            "keyboard": "keyboard",
            "zones": "zones",
            "live_draft": "live > draft",
            "effect": "effect                                      6/16",
            "color": "color",
            "motion": "motion",
        }
        for name, rect in layout.panels.items():
            if name != "footer":
                box(rect, titles[name])
        put(49, 5, FOOTER_ROWS[0])
        put(51, 5, FOOTER_ROWS[1])
    elif layout is PROFILES_LAYOUT:
        put(1, 3, "okeylitctl v0.2.0  >  Profiles")
        badges = "[ IN SYNC ]  [ DEVICE ON ]"
        put(1, layout.width - 3 - len(badges), badges)
        box(layout.panel("profiles"), "profiles")
        box(layout.panel("preview"), "preview · sunset")
        put(39, 5, REFERENCE_STATES["profiles"]["actions"])
    else:
        raise ValueError("unknown reference layout")

    return "\n".join("".join(row).rstrip() for row in canvas)
