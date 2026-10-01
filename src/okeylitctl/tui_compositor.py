"""Pure terminal-cell compositor for the approved OKeyLitCtl editor mockup."""

from __future__ import annotations

from dataclasses import dataclass

from . import __version__
from .effects import EffectKind
from .keyboard_layout import render_block_keyboard
from .models import ColorLayout, FIRMWARE_ZONE_ORDER, Zone
from .tui_state import TuiState
from .tui_visual_contract import (
    EDITOR_LAYOUT,
    EFFECT_NAMES,
    HEX_MODAL,
    PROFILES_LAYOUT,
    Rect,
)


@dataclass(frozen=True)
class ComposedScreen:
    width: int
    height: int
    lines: tuple[str, ...]
    roles: tuple[tuple[str, ...], ...]


class _Canvas:
    def __init__(self, width: int, height: int):
        self.width = width
        self.height = height
        self.characters = [[" " for _ in range(width)] for _ in range(height)]
        self.roles = [["background" for _ in range(width)] for _ in range(height)]

    def put(self, y: int, x: int, text: str, role: str = "text") -> None:
        if y < 0 or y >= self.height or x < 0 or x + len(text) > self.width:
            raise ValueError(
                f"write outside canvas: y={y}, x={x}, length={len(text)}, "
                f"canvas={self.width}x{self.height}"
            )
        for offset, character in enumerate(text):
            column = x + offset
            self.characters[y][column] = character
            self.roles[y][column] = role

    def fill(self, y: int, x: int, width: int, character: str, role: str) -> None:
        self.put(y, x, character * max(0, width), role)

    def box(self, rect: Rect, title: str, right_title: str = "") -> None:
        left, top = rect.x, rect.y
        right, bottom = rect.right - 1, rect.bottom - 1
        self.put(top, left, "┌" + "─" * (rect.width - 2) + "┐", "border")
        for row in range(top + 1, bottom):
            self.put(row, left, "│", "border")
            self.put(row, right, "│", "border")
        self.put(bottom, left, "└" + "─" * (rect.width - 2) + "┘", "border")
        self.put(top, left + 2, f" {title} ", "panel_title")
        if right_title:
            self.put(top, right - len(right_title) - 1, f"{right_title} ", "muted")

    def finish(self) -> ComposedScreen:
        return ComposedScreen(
            width=self.width,
            height=self.height,
            lines=tuple("".join(row) for row in self.characters),
            roles=tuple(tuple(row) for row in self.roles),
        )


def _draw_outer_frame(canvas: _Canvas) -> None:
    canvas.put(0, 0, "╭" + "─" * (canvas.width - 2) + "╮", "border")
    for row in range(1, canvas.height - 1):
        canvas.put(row, 0, "│", "border")
        canvas.put(row, canvas.width - 1, "│", "border")
    canvas.put(canvas.height - 1, 0, "╰" + "─" * (canvas.width - 2) + "╯", "border")


def _draw_header(canvas: _Canvas, state: TuiState, power_state: str, version: str) -> None:
    canvas.put(1, 3, "◆ okeylitctl", "brand")
    canvas.put(1, 16, f"v{version}", "muted")
    if state.effect_running and state.effect_restore_pending:
        status = "RESTORE PENDING"
        status_role = "error"
    elif state.effect_running and power_state == "off":
        status = "RESTORE PENDING"
        status_role = "error"
    elif state.effect_running and state.effect_frame_uncertain:
        status = "EFFECT UNKNOWN"
        status_role = "error"
    elif state.effect_running:
        status = "EFFECT ACTIVE"
        status_role = "effect_badge"
    else:
        status = "UNSAVED DRAFT" if state.dirty else "IN SYNC"
        status_role = "dirty_badge" if state.dirty else "synced_badge"
    status_text = f" ◆ {status} "
    canvas.put(1, 111, status_text.ljust(23), status_role)
    device = f" ● DEVICE {power_state.upper()} "
    canvas.put(1, 137, device.ljust(19), "device_badge")


def _draw_keyboard(canvas: _Canvas, state: TuiState) -> None:
    rect = EDITOR_LAYOUT.panel("keyboard")
    canvas.box(rect, "KEYBOARD")
    keyboard = render_block_keyboard(103, 19, state.selected_zone)
    origin_x = rect.x + 2
    origin_y = rect.y + 1
    for row in range(keyboard.height):
        for column in range(keyboard.width):
            key_id = keyboard.key_ids[row][column]
            if key_id is None:
                continue
            zone = keyboard.zones[row][column]
            role = f"key_{zone.value}"
            if keyboard.lines[row][column] == "▀":
                role += "_top"
            if keyboard.selected[row][column]:
                role += "_selected"
            canvas.put(
                origin_y + row,
                origin_x + column,
                keyboard.lines[row][column],
                role,
            )


def _draw_zones(canvas: _Canvas, state: TuiState) -> None:
    rect = EDITOR_LAYOUT.panel("zones")
    canvas.box(rect, "ZONES")
    for index, zone in enumerate(FIRMWARE_ZONE_ORDER, start=1):
        row = rect.y + 1 + index
        selected = zone is state.selected_zone
        canvas.put(row, rect.x + 2, "▸" if selected else " ", "focus" if selected else "muted")
        canvas.put(row, rect.x + 5, f"{index}  {zone.value.upper():<7}", "text")
        canvas.put(row, rect.x + 19, "  ", f"swatch_{zone.value}")
        value = getattr(state.draft, zone.value)
        canvas.put(row, rect.x + 23, f"#{value}", "draft" if state.dirty else "muted")
        if selected and getattr(state.current, zone.value) != value:
            canvas.put(row, rect.right - 4, "●", "dirty")


def _rgb_delta(live: str, draft: str) -> tuple[int, int, int]:
    live_values = tuple(int(live[offset : offset + 2], 16) for offset in (0, 2, 4))
    draft_values = tuple(int(draft[offset : offset + 2], 16) for offset in (0, 2, 4))
    return tuple(draft_value - live_value for live_value, draft_value in zip(live_values, draft_values))


def _draw_live_draft(canvas: _Canvas, state: TuiState, power_state: str) -> None:
    rect = EDITOR_LAYOUT.panel("live_draft")
    canvas.box(rect, "LIVE ▸ DRAFT")
    zone = state.selected_zone
    power_off = state.effect_running and power_state == "off"
    uncertain = state.effect_running and state.effect_frame_uncertain and not power_off
    live_layout = (
        state.effect_frame
        if state.effect_running
        and state.effect_frame is not None
        and not uncertain
        and not power_off
        else state.current
    )
    live = getattr(live_layout, zone.value)
    draft = getattr(state.draft, zone.value)
    canvas.put(rect.y + 3, rect.x + 3, "LIVE", "muted")
    canvas.put(rect.y + 4, rect.x + 3, "DRAFT", "muted")
    if power_off:
        canvas.put(rect.y + 3, rect.x + 12, "       ", "error")
        canvas.put(rect.y + 3, rect.x + 21, "POWER OFF", "error")
    elif uncertain:
        canvas.put(rect.y + 3, rect.x + 12, "???????", "error")
        canvas.put(rect.y + 3, rect.x + 21, "UNKNOWN", "error")
    else:
        canvas.put(rect.y + 3, rect.x + 12, "       ", f"swatch_live_{zone.value}_{live}")
        canvas.put(rect.y + 3, rect.x + 21, f"#{live}", "muted")
    canvas.put(rect.y + 4, rect.x + 12, "       ", f"swatch_draft_{zone.value}_{draft}")
    canvas.put(rect.y + 4, rect.x + 21, f"#{draft}", "draft" if live != draft else "muted")
    if power_off:
        canvas.put(rect.y + 7, rect.x + 3, "Base restore pending · power on", "error")
    elif uncertain:
        canvas.put(rect.y + 7, rect.x + 3, "Device state unknown · S Verify", "error")
    elif state.effect_restore_pending and state.effect_frame is not None:
        canvas.put(rect.y + 7, rect.x + 3, "Verified frame · S Restore", "focus")
    elif state.effect_running and state.effect_frame is not None:
        canvas.put(rect.y + 7, rect.x + 3, "Effect frame active · S Stop", "focus")
    elif live == draft:
        canvas.put(rect.y + 7, rect.x + 3, "No changes", "muted")
    else:
        red, green, blue = _rgb_delta(live, draft)
        canvas.put(rect.y + 7, rect.x + 3, f"Δ  R{red:+d}   G{green:+d}   B{blue:+d}", "dirty")


def _draw_effects(canvas: _Canvas, state: TuiState) -> None:
    rect = EDITOR_LAYOUT.panel("effect")
    canvas.box(rect, "EFFECT", f"{state.effect_index + 1}/16")
    starts = (12, 31, 50, 69, 88, 107, 126, 143)
    for index, name in enumerate(EFFECT_NAMES):
        row = rect.y + 3 + (index // 8) * 2
        text = name.upper()
        column = starts[index % 8] - len(text) // 2
        canvas.put(
            row,
            column,
            text,
            "effect_selected" if index == state.effect_index else "muted",
        )
    preview = "█" * (rect.width - 4)
    preview_role = f"effect_preview_{state.effect_kind.value.lower()}"
    canvas.put(rect.y + 7, rect.x + 2, preview, preview_role)
    canvas.put(rect.y + 8, rect.x + 2, preview, preview_role)


def _channel_values(color: str) -> tuple[int, int, int]:
    return tuple(int(color[offset : offset + 2], 16) for offset in (0, 2, 4))


def _draw_color(canvas: _Canvas, state: TuiState) -> None:
    rect = EDITOR_LAYOUT.panel("color")
    if state.effect_kind is EffectKind.CYCLE:
        canvas.box(rect, "COLOR · SPECTRUM")
        canvas.put(rect.y + 3, rect.x + 3, "▸ FROM", "focus")
        canvas.put(rect.y + 4, rect.x + 5, "TO", "muted")
        canvas.put(rect.y + 5, rect.x + 5, "SAT", "muted")
        spectrum = "█" * 57
        canvas.put(rect.y + 3, rect.x + 15, spectrum, "effect_preview_cycle")
        canvas.put(rect.y + 4, rect.x + 15, spectrum, "effect_preview_cycle")
        canvas.put(rect.y + 5, rect.x + 15, "━" * 51 + "─" * 6, "focus")
        canvas.put(rect.y + 3, rect.x + 76, "  0°", "text")
        canvas.put(rect.y + 4, rect.x + 76, "360°", "muted")
        canvas.put(rect.y + 5, rect.x + 76, " 90%", "muted")
        canvas.put(rect.y + 2, rect.x + 86, " H 190° ", "swatch_center")
        canvas.put(rect.y + 8, rect.x + 3, "PRESET  ‹ Rainbow ›", "muted")
        for index, role in enumerate(("right", "center", "left", "wasd", "center")):
            canvas.put(rect.y + 8, rect.x + 38 + index * 7, "     ", f"swatch_{role}")
        return

    zone = state.selected_zone
    canvas.box(rect, f"COLOR · {zone.value.upper()}")
    color = getattr(state.draft, zone.value)
    channels = (("RED", "channel_red"), ("GREEN", "channel_green"), ("BLUE", "channel_blue"))
    for index, ((label, role), value) in enumerate(zip(channels, _channel_values(color))):
        row = rect.y + 3 + index
        marker = "▸" if index == state.selected_channel else " "
        canvas.put(row, rect.x + 3, marker, "focus")
        canvas.put(row, rect.x + 6, f"{label:<5}", role if index == state.selected_channel else "muted")
        bar_width = 57
        filled = round(value / 255 * bar_width)
        canvas.put(row, rect.x + 15, "━" * filled + "─" * (bar_width - filled), role)
        canvas.put(row, rect.x + 76, f"{value:3d}  {value:02X}", "text")
    preset = "Aurora" if state.draft.to_wire() == "7B2CFF,00CFFF,FF2E88,E8F7FF" else "Custom"
    canvas.put(rect.y + 8, rect.x + 3, f"PRESET  ‹ {preset} ›", "muted")
    canvas.put(rect.y + 8, rect.x + 75, f"HEX #{color}", "muted")
    canvas.put(rect.y + 2, rect.x + 86, f" #{color} ", f"swatch_{zone.value}")


def _draw_motion(canvas: _Canvas, state: TuiState) -> None:
    rect = EDITOR_LAYOUT.panel("motion")
    canvas.box(rect, "MOTION")
    speed_percent = round(state.effect_speed * 100)
    light_percent = round(state.effect_light * 100)
    speed_filled = round(state.effect_speed * 12)
    light_filled = round(state.effect_light * 12)
    canvas.put(rect.y + 3, rect.x + 3, "SPEED", "text")
    canvas.put(rect.y + 4, rect.x + 3, "LIGHT", "text")
    canvas.put(rect.y + 3, rect.x + 12, "━" * speed_filled + "─" * (12 - speed_filled), "focus")
    canvas.put(rect.y + 4, rect.x + 12, "━" * light_filled + "─" * (12 - light_filled), "focus")
    canvas.put(rect.y + 3, rect.x + 31, f"{speed_percent:3d}%", "muted")
    canvas.put(rect.y + 4, rect.x + 31, f"{light_percent:3d}%", "muted")
    direction = "left → right" if state.effect_direction == 1 else "right → left"
    canvas.put(rect.y + 7, rect.x + 3, f"DIR   ‹ {direction} ›", "muted")


def _draw_footer(canvas: _Canvas, state: TuiState) -> None:
    first = "↔  zone    ↑↓  channel    + -  adjust    Tab  effect    [ ]  speed    E  hex    P  preset"
    if state.effect_running:
        second = "S Stop    X  discard    O  original    M  profiles    R  refresh    ?  help    Q  quit"
    else:
        second = "A  apply    X  discard    O  original    M  profiles    R  refresh    ?  help    Q  quit"
    canvas.put(49, 5, first, "footer")
    canvas.put(51, 5, second, "footer")


def compose_editor(
    state: TuiState,
    *,
    power_state: str,
    version: str = __version__,
) -> ComposedScreen:
    """Compose the approved reference editor without touching curses or hardware."""
    canvas = _Canvas(EDITOR_LAYOUT.width, EDITOR_LAYOUT.height)
    _draw_outer_frame(canvas)
    _draw_header(canvas, state, power_state, version)
    _draw_keyboard(canvas, state)
    _draw_zones(canvas, state)
    _draw_live_draft(canvas, state, power_state)
    _draw_effects(canvas, state)
    _draw_color(canvas, state)
    _draw_motion(canvas, state)
    _draw_footer(canvas, state)
    return canvas.finish()


def _draw_profiles_header(
    canvas: _Canvas,
    state: TuiState,
    power_state: str,
    version: str,
) -> None:
    canvas.put(1, 3, f"◆ okeylitctl v{version}  ›  Profiles", "brand")
    status = "UNSAVED DRAFT" if state.dirty else "IN SYNC"
    status_role = "dirty_badge" if state.dirty else "synced_badge"
    canvas.put(1, 111, f" ◆ {status} ".ljust(23), status_role)
    canvas.put(
        1,
        137,
        f" ● DEVICE {power_state.upper()} ".ljust(19),
        "device_badge",
    )


def _draw_profile_list(
    canvas: _Canvas,
    names: tuple[str, ...],
    selected: int,
) -> None:
    rect = PROFILES_LAYOUT.panel("profiles")
    canvas.box(rect, "PROFILES")
    canvas.put(rect.y + 2, rect.x + 3, f"SAVED  {len(names):02d}", "muted")
    if not names:
        canvas.put(rect.y + 5, rect.x + 3, "No saved profiles", "muted")
        return
    visible = rect.height - 8
    start = max(0, min(selected - visible + 1, len(names) - visible))
    for row, name in enumerate(names[start : start + visible]):
        index = start + row
        is_selected = index == selected
        role = "profile_selected" if is_selected else "text"
        marker = "▸" if is_selected else " "
        canvas.put(rect.y + 5 + row, rect.x + 3, f"{marker} {name:<34}", role)


def _draw_profile_preview(
    canvas: _Canvas,
    name: str | None,
    layout: ColorLayout | None,
    error: str,
    message: str,
) -> None:
    rect = PROFILES_LAYOUT.panel("preview")
    title = f"PREVIEW · {name.upper()}" if name else "PREVIEW"
    canvas.box(rect, title)
    if error:
        canvas.put(rect.y + 5, rect.x + 4, "Preview unavailable", "profile_error")
        canvas.put(rect.y + 7, rect.x + 4, error[: rect.width - 8], "muted")
    elif layout is None:
        canvas.put(rect.y + 5, rect.x + 4, "Select a saved profile to preview", "muted")
    else:
        keyboard = render_block_keyboard(100, 19, Zone.WASD)
        origin_x = rect.x + 2
        origin_y = rect.y + 2
        for row in range(keyboard.height):
            for column in range(keyboard.width):
                if keyboard.key_ids[row][column] is None:
                    continue
                zone = keyboard.zones[row][column]
                color = getattr(layout, zone.value)
                top = "_top" if keyboard.lines[row][column] == "▀" else ""
                canvas.put(
                    origin_y + row,
                    origin_x + column,
                    keyboard.lines[row][column],
                    f"profile_key_{zone.value}{top}_{color}",
                )
        for index, zone in enumerate(FIRMWARE_ZONE_ORDER):
            column = rect.x + 3 + index * 24
            color = getattr(layout, zone.value)
            canvas.put(rect.y + 23, column, zone.value.upper(), "muted")
            canvas.put(
                rect.y + 24,
                column,
                "    ",
                f"profile_color_{zone.value}_{color}",
            )
            canvas.put(rect.y + 24, column + 6, f"#{color}", "text")
    if message:
        canvas.put(rect.bottom - 3, rect.x + 3, message[: rect.width - 6], "muted")


def compose_profiles(
    state: TuiState,
    *,
    names: tuple[str, ...],
    selected: int,
    preview_layout: ColorLayout | None,
    power_state: str,
    preview_error: str = "",
    message: str = "",
    pending_delete: str | None = None,
    version: str = __version__,
) -> ComposedScreen:
    """Compose the approved split Profiles list and preview screen."""
    canvas = _Canvas(PROFILES_LAYOUT.width, PROFILES_LAYOUT.height)
    _draw_outer_frame(canvas)
    _draw_profiles_header(canvas, state, power_state, version)
    _draw_profile_list(canvas, names, selected)
    name = names[selected] if names and 0 <= selected < len(names) else None
    _draw_profile_preview(canvas, name, preview_layout, preview_error, message)
    if pending_delete is not None:
        profiles_rect = PROFILES_LAYOUT.panel("profiles")
        prompt_width = profiles_rect.width - 6
        canvas.put(33, profiles_rect.x + 3, "Delete profile?", "profile_error")
        canvas.put(
            34,
            profiles_rect.x + 3,
            pending_delete[:prompt_width],
            "profile_error",
        )
        canvas.put(35, profiles_rect.x + 3, "Y confirm · N cancel", "profile_selected")
    footer = (
        "↑↓  select    Enter  load into draft    S  save current as    "
        "N  rename    D  delete    Esc  back"
    )
    canvas.put(39, 5, footer, "footer")
    return canvas.finish()


def compose_exact_hex_modal(
    state: TuiState,
    *,
    power_state: str,
    input_text: str,
    error: str,
    version: str = __version__,
) -> ComposedScreen:
    """Compose the guided exact-color modal over a dimmed editor."""
    base = compose_editor(state, power_state=power_state, version=version)
    canvas = _Canvas(base.width, base.height)
    canvas.characters = [list(row) for row in base.lines]
    canvas.roles = [
        [role if role.startswith("dimmed_") else f"dimmed_{role}" for role in row]
        for row in base.roles
    ]

    rect = HEX_MODAL
    for row in range(rect.y, rect.bottom):
        canvas.put(row, rect.x, " " * rect.width, "modal_fill")
    right = rect.right - 1
    bottom = rect.bottom - 1
    canvas.put(rect.y, rect.x, "┌" + "─" * (rect.width - 2) + "┐", "modal_border")
    for row in range(rect.y + 1, bottom):
        canvas.put(row, rect.x, "│", "modal_border")
        canvas.put(row, right, "│", "modal_border")
    canvas.put(bottom, rect.x, "└" + "─" * (rect.width - 2) + "┘", "modal_border")

    zone = state.selected_zone
    live = getattr(state.current, zone.value)
    normalized = input_text.upper()
    valid = len(normalized) == 6 and all(character in "0123456789ABCDEF" for character in normalized)
    canvas.put(rect.y, rect.x + 2, f" EXACT HEX · {zone.value.upper()} ", "modal_title")
    canvas.put(rect.y + 3, rect.x + 6, f"# {normalized}", "modal_input")
    underline_width = 32
    canvas.put(rect.y + 4, rect.x + 6, "─" * underline_width, "modal_input")
    canvas.put(
        rect.y + 6,
        rect.x + 6,
        f"× {error}" if error else ("✓ ready" if valid else "waiting for six digits"),
        "modal_error" if error else ("modal_ready" if valid else "modal_muted"),
    )

    canvas.put(rect.y + 10, rect.x + 6, "LIVE", "modal_muted")
    canvas.put(rect.y + 11, rect.x + 6, "NEW", "modal_muted")
    canvas.put(rect.y + 10, rect.x + 13, "        ", f"modal_swatch_live_{live}")
    canvas.put(rect.y + 10, rect.x + 23, f"#{live}", "modal_text")
    if valid:
        canvas.put(rect.y + 11, rect.x + 13, "        ", f"modal_swatch_new_{normalized}")
        canvas.put(rect.y + 11, rect.x + 23, f"#{normalized}", "modal_text")
    else:
        canvas.put(rect.y + 11, rect.x + 13, "........", "modal_muted")
        canvas.put(rect.y + 11, rect.x + 23, "waiting…", "modal_muted")

    canvas.put(rect.y + 15, rect.x + 6, "Enter", "modal_shortcut")
    canvas.put(rect.y + 15, rect.x + 12, "accept", "modal_muted")
    canvas.put(rect.y + 15, rect.x + 23, "Esc", "modal_shortcut")
    canvas.put(rect.y + 15, rect.x + 27, "cancel", "modal_muted")
    canvas.put(rect.y + 15, rect.x + 38, "^U", "modal_shortcut")
    canvas.put(rect.y + 15, rect.x + 41, "clear", "modal_muted")
    return canvas.finish()
