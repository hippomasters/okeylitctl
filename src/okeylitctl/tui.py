"""Dependency-free curses TUI for safe, deliberate lighting changes."""

from __future__ import annotations

import curses
from enum import Enum, auto
from time import monotonic, sleep
from typing import Any

from . import __version__
from .effect_runtime import EffectRuntime
from .effects import EffectEvent, EffectKind, PHYSICAL_ZONE_ORDER, frame_at
from .keyboard_layout import key_zone_by_id
from .models import ColorLayout, FIRMWARE_ZONE_ORDER, Zone
from .profiles import ProfileError, ProfileStore
from .sysfs import SysfsBackend, SysfsError
from .tui_compositor import (
    SPECTRUM_BANDS, compose_adaptive_editor, compose_editor, compose_exact_hex_modal,
    compose_narrow_editor, compose_profiles, compose_responsive_hex_modal,
    compose_responsive_profiles,
)
from .tui_state import TuiState
from .validation import ValidationError


PRESETS = (
    ("Aurora", ColorLayout.from_wire("7B2CFF,00CFFF,FF2E88,E8F7FF")),
    ("Ocean", ColorLayout.from_wire("0057FF,00B8D9,003B73,7FDBFF")),
    ("Ember", ColorLayout.from_wire("FF3B00,FF8C00,8B0000,FFD166")),
    ("Matrix", ColorLayout.from_wire("003B00,00A828,001A00,39FF14")),
    ("Ice", ColorLayout.from_wire("8EDBFF,DFF8FF,4EA8DE,FFFFFF")),
    ("Mono", ColorLayout.from_wire("D8DEE9,D8DEE9,D8DEE9,FFFFFF")),
)


class TuiCommand(Enum):
    NONE = auto()
    APPLY = auto()
    RESTORE = auto()
    REFRESH = auto()
    EDIT = auto()
    PROFILES = auto()
    STOP = auto()
    QUIT = auto()


def rgb_to_xterm_index(color: str) -> int:
    """Approximate RRGGBB with the xterm 6x6x6 color cube."""
    channels = [int(color[offset : offset + 2], 16) for offset in (0, 2, 4)]
    cube = [round(channel / 255 * 5) for channel in channels]
    return 16 + 36 * cube[0] + 6 * cube[1] + cube[2]


def _blend_rgb(color: str, target: str, amount: float) -> str:
    """Blend one validated RRGGBB color toward another."""
    channels = []
    for offset in (0, 2, 4):
        source = int(color[offset : offset + 2], 16)
        destination = int(target[offset : offset + 2], 16)
        channels.append(round(source + (destination - source) * amount))
    return "".join(f"{channel:02X}" for channel in channels)


def _key_surface_colors(color: str) -> tuple[str, str, str]:
    """Return body text, top highlight, and dark body colors for a keycap."""
    return (
        _blend_rgb(color, "FFFFFF", 0.55),
        _blend_rgb(color, "FFFFFF", 0.15),
        _blend_rgb(color, "0B0E18", 0.65),
    )


def set_cursor_visibility(visibility: int) -> None:
    """Set cursor visibility when the terminal supports it."""
    try:
        curses.curs_set(visibility)
    except curses.error:
        pass


def initialize_colors() -> bool:
    """Initialize optional terminal colors, falling back to monochrome."""
    try:
        if not curses.has_colors():
            return False
    except curses.error:
        return False
    try:
        curses.start_color()
    except curses.error:
        return False

    background = -1
    try:
        curses.use_default_colors()
    except curses.error:
        background = curses.COLOR_BLACK

    try:
        curses.init_pair(1, curses.COLOR_CYAN, background)
        curses.init_pair(2, curses.COLOR_BLACK, curses.COLOR_CYAN)
        curses.init_pair(3, curses.COLOR_GREEN, background)
        curses.init_pair(4, curses.COLOR_YELLOW, background)
        curses.init_pair(5, curses.COLOR_RED, background)
    except curses.error:
        return False
    return True


_CHARACTER_KEY_IDS = {
    "`": "GRAVE", "~": "GRAVE",
    **{str(number): f"DIGIT{number}" for number in range(1, 10)},
    "0": "DIGIT0",
    "!": "DIGIT1", "@": "DIGIT2", "#": "DIGIT3", "$": "DIGIT4",
    "%": "DIGIT5", "^": "DIGIT6", "&": "DIGIT7", "*": "DIGIT8",
    "(": "DIGIT9", ")": "DIGIT0",
    "-": "MINUS", "_": "MINUS", "=": "EQUAL", "+": "EQUAL",
    "[": "LBRACKET", "{": "LBRACKET", "]": "RBRACKET", "}": "RBRACKET",
    "\\": "BACKSLASH", "|": "BACKSLASH",
    ";": "SEMICOLON", ":": "SEMICOLON", "'": "APOSTROPHE", '"': "APOSTROPHE",
    ",": "COMMA", "<": "COMMA", ".": "PERIOD", ">": "PERIOD",
    "/": "SLASH", "?": "SLASH", " ": "SPACE", "\t": "TAB",
    "\n": "ENTER", "\r": "ENTER", "\b": "BACKSPACE", "\x7f": "BACKSPACE",
}
_CHARACTER_KEY_IDS.update({letter.lower(): letter for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"})
_CHARACTER_KEY_IDS.update({letter: letter for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"})

_SPECIAL_KEY_IDS = {
    curses.KEY_LEFT: "LEFT",
    curses.KEY_RIGHT: "RIGHT",
    curses.KEY_UP: "UP",
    curses.KEY_DOWN: "DOWN",
    curses.KEY_HOME: "KP7",
    curses.KEY_END: "KP1",
    curses.KEY_NPAGE: "KP3",
    curses.KEY_PPAGE: "KP9",
    curses.KEY_DC: "DELETE",
    curses.KEY_IC: "INSERT",
    curses.KEY_BACKSPACE: "BACKSPACE",
    curses.KEY_ENTER: "ENTER",
}
_SPECIAL_KEY_IDS.update(
    {curses.KEY_F0 + number: f"F{number}" for number in range(1, 13)}
)


def effect_zone_for_key(key: int, selected_zone: Zone) -> Zone:
    """Resolve terminal input through the authoritative physical-key zone model."""
    key_id = _SPECIAL_KEY_IDS.get(key)
    if key_id is None and 0 <= key < 256:
        key_id = _CHARACTER_KEY_IDS.get(chr(key))
    if key_id is None:
        return selected_zone
    return key_zone_by_id(key_id)


def handle_key(
    state: TuiState, key: int, *, actions_enabled: bool = True
) -> TuiCommand:
    """Apply a key to local UI state and return any deliberate device action."""
    if not actions_enabled:
        return TuiCommand.QUIT if key == ord("q") else TuiCommand.NONE

    if state.help_visible:
        if key in (27, ord("?"), ord("q")):
            state.help_visible = False
        return TuiCommand.NONE

    if key in (curses.KEY_RIGHT, ord("l")):
        state.select_next_zone()
    elif key == 9:
        state.select_next_effect()
    elif key in (curses.KEY_LEFT, ord("h")):
        state.select_previous_zone()
    elif key == curses.KEY_BTAB:
        state.select_previous_effect()
    elif key == curses.KEY_DOWN:
        state.selected_channel = (state.selected_channel + 1) % 3
    elif key == curses.KEY_UP:
        state.selected_channel = (state.selected_channel - 1) % 3
    elif key in (ord("+"), ord("=")):
        if state.effect_kind is EffectKind.CYCLE:
            state.adjust_cycle_control(1)
        else:
            state.adjust_selected_channel(1)
    elif key == ord("-"):
        if state.effect_kind is EffectKind.CYCLE:
            state.adjust_cycle_control(-1)
        else:
            state.adjust_selected_channel(-1)
    elif key == ord("]"):
        state.adjust_effect_speed(0.05)
    elif key == ord("["):
        state.adjust_effect_speed(-0.05)
    elif key == ord("}"):
        state.adjust_effect_light(0.05)
    elif key == ord("{"):
        state.adjust_effect_light(-0.05)
    elif key == ord("d"):
        state.toggle_effect_direction()
    elif key == ord("v"):
        state.compact_panel = "effects" if state.compact_panel == "color" else "color"
    elif key in (ord("1"), ord("2"), ord("3"), ord("4")):
        state.selected_zone = FIRMWARE_ZONE_ORDER[key - ord("1")]
    elif key == ord("p"):
        if state.effect_kind is EffectKind.CYCLE:
            name = state.select_next_cycle_preset()
            state.message = f"Cycle palette: {name} — preview only until A starts"
        else:
            state.preset_index = (state.preset_index + 1) % len(PRESETS)
            name, layout = PRESETS[state.preset_index]
            state.draft = layout
            if state.dirty:
                state.message = f"Preset loaded locally: {name} — press A to apply"
            else:
                state.message = f"Preset already active: {name}"
    elif key == ord("x"):
        state.discard_draft()
    elif key == ord("?"):
        state.help_visible = True
    elif key == ord("a"):
        return TuiCommand.APPLY
    elif key == ord("o"):
        return TuiCommand.RESTORE
    elif key == ord("r"):
        return TuiCommand.REFRESH
    elif key == ord("e"):
        return TuiCommand.EDIT
    elif key == ord("m"):
        return TuiCommand.PROFILES
    elif key == ord("s"):
        return TuiCommand.STOP
    elif key == ord("q"):
        return TuiCommand.QUIT
    return TuiCommand.NONE


class CursesTui:
    """Interactive adapter; all editing remains local until Apply."""

    def __init__(self, backend: SysfsBackend, profile_store: ProfileStore | None = None):
        self.backend = backend
        self.profile_store = profile_store or ProfileStore()
        self.power_state = "unknown"
        self.colors_enabled = False
        self._render_failed = False
        self._rendered_geometry: tuple[int, int] | None = None
        self._profile_rendered_geometry: tuple[int, int] | None = None
        self.effect_runtime: EffectRuntime | None = None
        self._terminal_cleanup_forced = False
        self.state = self._load_state()

    def _load_state(self) -> TuiState:
        status = self.backend.status()
        self.power_state = str(status["state"])
        return TuiState(
            current=ColorLayout.from_wire(",".join(status["colors"])),
            original=ColorLayout.from_wire(",".join(status["original"])),
        )

    def run(self) -> None:
        curses.wrapper(self._main)

    def _update_preview(self, *, now: float) -> None:
        """Animate only the local drawing; never access the device."""
        if self.state.effect_running or self.state.effect_kind is EffectKind.STATIC:
            self.state.preview_frame = None
        else:
            event = None
            if self.state.effect_kind in (EffectKind.REACTIVE, EffectKind.RIPPLE):
                period_start = now - now % 2.0
                zone = PHYSICAL_ZONE_ORDER[int(now // 2.0) % len(PHYSICAL_ZONE_ORDER)]
                event = EffectEvent(zone=zone, elapsed=period_start)
            self.state.preview_frame = frame_at(self.state.effect_spec, now, event=event)

    def _cleanup_after_terminal_failure(self) -> bool:
        delay = 0.05
        while self.effect_runtime is not None:
            runtime = self.effect_runtime
            was_uncertain = runtime.write_state_uncertain
            if self._stop_effect():
                return True
            runtime = self.effect_runtime
            if (
                was_uncertain
                and runtime is not None
                and not runtime.write_state_uncertain
                and runtime.last_written is not None
                and runtime.last_written != runtime.spec.base
            ):
                self.state.message = (
                    "Effect state verified; restoring base on the next bounded cleanup retry"
                )
            try:
                sleep(delay)
            except KeyboardInterrupt:
                self.state.message = (
                    "Exit forced with effect restoration unresolved; no unsafe write issued"
                )
                self._terminal_cleanup_forced = True
                return False
            delay = min(1.0, delay * 2.0)
        return True

    def _main(self, screen: Any) -> None:
        self._terminal_cleanup_forced = False
        try:
            screen.keypad(True)
        except curses.error:
            return
        try:
            screen.timeout(50)
        except (AttributeError, curses.error):
            pass
        set_cursor_visibility(0)
        self.colors_enabled = initialize_colors()

        try:
            while True:
                self._update_preview(now=monotonic())
                workspace_visible = self._draw(screen)
                rendered_geometry = self._rendered_geometry
                if rendered_geometry is None:
                    if self.effect_runtime is not None:
                        self._cleanup_after_terminal_failure()
                    return
                try:
                    if screen.getmaxyx() != rendered_geometry:
                        continue
                except curses.error:
                    if self.effect_runtime is not None:
                        self._cleanup_after_terminal_failure()
                    return
                try:
                    height, width = screen.getmaxyx()
                except curses.error:
                    if self.effect_runtime is not None:
                        self._cleanup_after_terminal_failure()
                    return
                actions_enabled = workspace_visible and height >= 24 and width >= 78
                if self.state.effect_running:
                    try:
                        self._tick_effect(now=monotonic(), authorized=actions_enabled)
                    except KeyboardInterrupt:
                        self.state.message = (
                            "Interrupt requested during firmware I/O; verifying state before exit"
                        )
                        if self.effect_runtime is not None and not self._stop_effect():
                            continue
                        return
                try:
                    key = screen.getch()
                except curses.error:
                    self.state.message = "Terminal input failed; waiting to restore effect base"
                    if self.effect_runtime is not None:
                        self._cleanup_after_terminal_failure()
                    return
                except KeyboardInterrupt:
                    self.state.message = "Interrupt requested; restoring effect base before exit"
                    if self.effect_runtime is not None and not self._stop_effect():
                        continue
                    return
                except StopIteration:
                    return
                if key == -1:
                    continue
                if key == curses.KEY_RESIZE:
                    continue
                try:
                    height, width = screen.getmaxyx()
                except curses.error:
                    if self.effect_runtime is not None:
                        self._cleanup_after_terminal_failure()
                    return
                if (height, width) != rendered_geometry:
                    continue
                if not actions_enabled and key != ord("q"):
                    continue
                if self.state.help_visible:
                    handle_key(self.state, key, actions_enabled=actions_enabled)
                    continue
                if self.state.effect_running and actions_enabled and key == ord("v"):
                    handle_key(self.state, key, actions_enabled=True)
                    continue
                if self.state.effect_running and key not in (
                    ord("s"),
                    ord("q"),
                    ord("?"),
                    27,
                ):
                    if self._trigger_effect_input(key, now=monotonic()):
                        continue
                    self.state.message = "Stop the running effect before editing"
                    continue
                command = handle_key(
                    self.state,
                    key,
                    actions_enabled=actions_enabled,
                )
                if command is TuiCommand.QUIT:
                    if self.state.dirty:
                        if not self._confirm_action(screen, "Quit?"):
                            self.state.message = "Quit cancelled — local changes preserved"
                            continue
                    if self.effect_runtime is not None and not self._stop_effect():
                        continue
                    return
                if command is TuiCommand.APPLY:
                    if self.state.effect_kind.value == "Static":
                        self._apply()
                    else:
                        self._start_effect(now=monotonic())
                elif command is TuiCommand.STOP:
                    self._stop_effect()
                elif command is TuiCommand.RESTORE:
                    if self._confirm_action(screen, "Restore the original module layout?"):
                        self._restore()
                    else:
                        self.state.message = "Restore cancelled"
                elif command is TuiCommand.REFRESH:
                    self._refresh()
                elif command is TuiCommand.EDIT:
                    self._edit_color(screen)
                elif command is TuiCommand.PROFILES:
                    self._profile_manager(screen)
        finally:
            if self.effect_runtime is not None and not self._terminal_cleanup_forced:
                self._stop_effect()

    def _safe_add(
        self,
        screen: Any,
        y: int,
        x: int,
        text: str,
        attr: int = 0,
        *,
        require_full: bool = False,
    ) -> bool:
        try:
            height, width = screen.getmaxyx()
            available = max(0, width - x - 1)
            if y < 0 or y >= height or x < 0 or x >= width:
                self._render_failed = True
                return False
            if require_full and len(text) > available:
                self._render_failed = True
                return False
            screen.addnstr(y, x, text, available, attr)
            return True
        except curses.error:
            self._render_failed = True
            return False

    def _draw(self, screen: Any) -> bool:
        self._render_failed = False
        self._rendered_geometry = None
        try:
            screen.erase()
            height, width = screen.getmaxyx()
        except curses.error:
            return False
        self._rendered_geometry = (height, width)
        if height < 24 or width < 78:
            self._safe_add(screen, 1, 2, "OKeyLitCtl needs at least 78 x 24", curses.A_BOLD)
            self._safe_add(screen, 3, 2, f"Current terminal: {width} x {height}")
            self._safe_add(screen, 5, 2, "Resize the terminal or press Q to quit.")
            try:
                screen.refresh()
            except curses.error:
                self._render_failed = True
            return not self._render_failed

        if (height, width) == (55, 160):
            return self._draw_reference_editor(screen, height, width)

        if height >= 30 and width >= 100:
            composed = compose_adaptive_editor(
                self.state, width=width, height=height,
                power_state=self.power_state, version=__version__,
            )
            return self._draw_composed_screen(screen, composed, height, width)

        composed = compose_narrow_editor(
            self.state, width=width, height=height,
            power_state=self.power_state, version=__version__,
        )
        return self._draw_composed_screen(screen, composed, height, width)

    def _reference_role_attr(self, role: str) -> int:
        if role.startswith("dimmed_"):
            return self._reference_role_attr(role.removeprefix("dimmed_")) | curses.A_DIM
        if role.startswith("profile_key_"):
            zone_name, color = role.removeprefix("profile_key_").rsplit("_", 1)
            top = zone_name.endswith("_top")
            zone = Zone(zone_name.removesuffix("_top"))
            return self._key_attr(
                zone, top=top, selected=False, color_override=color, pair_base=38
            )
        if role.startswith("profile_color_"):
            zone_name, color = role.removeprefix("profile_color_").split("_", 1)
            zone = Zone(zone_name)
            pair_number = 26 + FIRMWARE_ZONE_ORDER.index(zone)
            return self._color_attr(color, pair_number=pair_number)
        if role.startswith("effect_color_"):
            try:
                zone_name, color = role.removeprefix("effect_color_").rsplit("_", 1)
                pair_number = 100 + FIRMWARE_ZONE_ORDER.index(Zone(zone_name))
                foreground = rgb_to_xterm_index(color)
            except ValueError:
                return curses.A_DIM
            if (
                not self.colors_enabled or getattr(curses, "COLORS", 0) < 256
                or getattr(curses, "COLOR_PAIRS", 0) <= pair_number
            ):
                return curses.A_BOLD
            try:
                curses.init_pair(pair_number, foreground, curses.COLOR_BLACK)
                return curses.color_pair(pair_number)
            except curses.error:
                return curses.A_BOLD
        if role.startswith("spectrum_"):
            try:
                band_text, color = role.removeprefix("spectrum_").split("_", 1)
                band = int(band_text)
                if not 0 <= band < SPECTRUM_BANDS or len(color) != 6:
                    raise ValueError("invalid spectrum role")
                foreground = rgb_to_xterm_index(color)
            except ValueError:
                return curses.A_DIM
            pair_number = 50 + band
            if (
                not self.colors_enabled or getattr(curses, "COLORS", 0) < 256
                or getattr(curses, "COLOR_PAIRS", 0) < 50 + SPECTRUM_BANDS
                or getattr(curses, "COLOR_PAIRS", 0) <= pair_number
            ):
                return curses.A_BOLD
            try:
                curses.init_pair(pair_number, foreground, curses.COLOR_BLACK)
                return curses.color_pair(pair_number)
            except curses.error:
                return curses.A_BOLD
        if role == "key_unavailable":
            return curses.A_DIM
        if role.startswith("key_"):
            selected = role.endswith("_selected")
            key_role = role.removeprefix("key_").removesuffix("_selected")
            top = key_role.endswith("_top")
            zone_name, separator, color = key_role.removesuffix("_top").partition("_")
            try:
                return self._key_attr(
                    Zone(zone_name), top=top, selected=selected,
                    color_override=color if separator else None,
                )
            except ValueError:
                return curses.A_DIM
        if role.startswith("swatch_live_"):
            return self._color_attr(role.rsplit("_", 1)[1], pair_number=14)
        if role.startswith("swatch_draft_"):
            return self._color_attr(role.rsplit("_", 1)[1], pair_number=15)
        if role.startswith("modal_swatch_live_"):
            return self._color_attr(role.rsplit("_", 1)[1], pair_number=17)
        if role.startswith("modal_swatch_new_"):
            return self._color_attr(role.rsplit("_", 1)[1], pair_number=18)
        if role.startswith("swatch_"):
            for zone in Zone:
                if f"_{zone.value}" in role:
                    color = getattr(self.state.draft, zone.value)
                    return self._color_attr(
                        color, pair_number=22 + FIRMWARE_ZONE_ORDER.index(zone)
                    )
        if role in {
            "dirty_badge",
            "error",
            "dirty",
            "modal_error",
            "profile_error",
        }:
            base = curses.color_pair(4) if self.colors_enabled else 0
            return base | curses.A_REVERSE | curses.A_BOLD
        if role in {"modal_border", "modal_title", "modal_input", "modal_shortcut"}:
            base = curses.color_pair(1) if self.colors_enabled else 0
            return base | curses.A_BOLD
        if role in {"modal_fill", "modal_swatch_live", "modal_swatch_new"}:
            return curses.A_REVERSE
        if role == "modal_ready":
            base = curses.color_pair(3) if self.colors_enabled else 0
            return base | curses.A_BOLD
        if role == "modal_muted":
            return curses.A_DIM
        if role == "synced_badge":
            base = curses.color_pair(3) if self.colors_enabled else 0
            return base | curses.A_REVERSE | curses.A_BOLD
        if role in {
            "device_badge",
            "effect_badge",
            "effect_selected",
            "focus",
            "profile_selected",
        }:
            base = curses.color_pair(2) if self.colors_enabled else 0
            return base | curses.A_REVERSE | curses.A_BOLD
        if role in {"brand", "panel_title", "draft"}:
            base = curses.color_pair(1) if self.colors_enabled else 0
            return base | curses.A_BOLD
        if role == "channel_red":
            return (curses.color_pair(5) if self.colors_enabled else 0) | curses.A_BOLD
        if role == "channel_green":
            return (curses.color_pair(3) if self.colors_enabled else 0) | curses.A_BOLD
        if role == "channel_blue" or role.startswith("effect_preview"):
            return (curses.color_pair(1) if self.colors_enabled else 0) | curses.A_BOLD
        if role in {"border", "muted"}:
            return curses.A_DIM
        if role == "footer":
            return curses.A_DIM
        return 0

    def _draw_composed_screen(
        self,
        screen: Any,
        composed: Any,
        height: int,
        width: int,
    ) -> bool:
        origin_y = max(0, (height - composed.height) // 2)
        origin_x = max(0, (width - composed.width) // 2)
        for row_index, line in enumerate(composed.lines):
            roles = composed.roles[row_index]
            start = 0
            while start < composed.width:
                role = roles[start]
                end = start + 1
                while end < composed.width and roles[end] == role:
                    end += 1
                draw_y = origin_y + row_index
                draw_x = origin_x + start
                text = line[start:end]
                attr = self._reference_role_attr(role)
                if (
                    draw_y < 0
                    or draw_y >= height
                    or draw_x < 0
                    or draw_x + len(text) > width
                ):
                    self._render_failed = True
                    return False
                try:
                    if draw_y == height - 1 and draw_x + len(text) == width and text:
                        if len(text) > 1:
                            screen.addnstr(draw_y, draw_x, text[:-1], len(text) - 1, attr)
                        try:
                            screen.insstr(draw_y, width - 1, text[-1], attr)
                        except AttributeError:
                            screen.addnstr(draw_y, width - 1, text[-1], 1, attr)
                    else:
                        screen.addnstr(draw_y, draw_x, text, len(text), attr)
                except (curses.error, OverflowError):
                    self._render_failed = True
                    return False
                start = end
        if self._render_failed:
            return False
        try:
            screen.refresh()
        except curses.error:
            self._render_failed = True
        return not self._render_failed

    def _draw_reference_editor(self, screen: Any, height: int, width: int) -> bool:
        composed = compose_editor(
            self.state,
            power_state=self.power_state,
            version=__version__,
        )
        return self._draw_composed_screen(screen, composed, height, width)

    def _color_attr(
        self,
        color: str,
        *,
        pair_number: int,
        selected: bool = False,
    ) -> int:
        fallback = curses.A_REVERSE if selected else curses.A_DIM
        if (
            not self.colors_enabled
            or getattr(curses, "COLORS", 0) < 256
            or getattr(curses, "COLOR_PAIRS", 0) <= pair_number
        ):
            return fallback
        background = rgb_to_xterm_index(color)
        luminance = sum(
            int(color[offset : offset + 2], 16) * weight
            for offset, weight in zip((0, 2, 4), (299, 587, 114))
        ) / 1000
        foreground = 16 if luminance > 145 else 231
        try:
            curses.init_pair(pair_number, foreground, background)
            attr = curses.color_pair(pair_number)
            return attr | (curses.A_BOLD if selected else 0)
        except curses.error:
            return fallback

    def _key_attr(
        self,
        zone: Zone,
        *,
        top: bool = False,
        selected: bool | None = None,
        color_override: str | None = None,
        pair_base: int = 30,
    ) -> int:
        if selected is None:
            selected = zone is self.state.selected_zone
        color = (
            color_override
            if color_override is not None
            else getattr(self.state.draft, zone.value)
        )
        fallback = (
            curses.A_REVERSE | curses.A_BOLD
            if selected and not top
            else curses.A_BOLD if top else 0
        )
        if not self.colors_enabled or getattr(curses, "COLORS", 0) < 256:
            return fallback
        body_foreground, top_foreground, background = _key_surface_colors(color)
        zone_index = FIRMWARE_ZONE_ORDER.index(zone)
        pair_number = pair_base + zone_index + (4 if top else 0)
        if getattr(curses, "COLOR_PAIRS", 0) <= pair_number:
            return fallback
        try:
            curses.init_pair(
                pair_number,
                rgb_to_xterm_index(top_foreground if top else body_foreground),
                rgb_to_xterm_index(background),
            )
            return curses.color_pair(pair_number) | (curses.A_BOLD if selected else 0)
        except curses.error:
            return fallback

    def _draw_help(self, screen: Any) -> None:
        try:
            height, width = screen.getmaxyx()
        except curses.error:
            return
        lines = (
            "OKEYLITCTL HELP",
            "",
            "1–4 / ← →    Select a named keyboard zone",
            "↑ ↓           Select red, green, or blue channel",
            "+ -           Adjust the selected RGB channel by one",
            "Tab / Shift-Tab  Select next or previous effect",
            "[ ]           Adjust effect speed",
            "{ }           Adjust effect light",
            "D             Reverse effect direction",
            "S             Stop a running effect and restore its base",
            "E             Enter an exact six-digit hex color",
            "P             Cycle curated local presets",
            "M             Open the local profile manager",
            "A             Apply and verify the complete layout immediately",
            "O             Confirm restoration of the original layout",
            "X             Discard unapplied local edits",
            "R             Refresh from the device (blocked while dirty)",
            "Q             Quit; dirty drafts require confirmation",
            "",
            "No color-picker movement writes firmware. Apply is always explicit.",
            "Press ? or Esc to close",
        )
        box_width = min(72, width - 6)
        box_height = len(lines) + 2
        y = max(1, (height - box_height) // 2)
        x = max(2, (width - box_width) // 2)
        for row in range(box_height):
            self._safe_add(screen, y + row, x, " " * box_width, curses.A_REVERSE)
        for row, line in enumerate(lines):
            attr = curses.A_REVERSE | (curses.A_BOLD if row == 0 else 0)
            self._safe_add(screen, y + 1 + row, x + 2, line, attr)

    def _profile_manager(self, screen: Any) -> None:
        selected = 0
        pending_delete: str | None = None
        names: tuple[str, ...] = ()
        preview_layout: ColorLayout | None = None
        preview_error = ""
        preview_name: str | None = None
        try:
            names = self.profile_store.list_names()
        except ProfileError as exc:
            preview_error = f"Profile manager failed: {exc}"

        while True:
            if names:
                selected = min(selected, len(names) - 1)
            else:
                selected = 0
            current_name = names[selected] if names else None
            if current_name != preview_name:
                preview_name = current_name
                preview_layout = None
                if current_name is not None:
                    try:
                        preview_layout = self.profile_store.load(current_name)
                        preview_error = ""
                    except ProfileError as exc:
                        preview_error = f"Profile preview failed: {exc}"
            if not self._draw_profile_manager(
                screen,
                names,
                selected,
                pending_delete,
                preview_layout=preview_layout,
                preview_error=preview_error,
            ):
                self.state.message = "Profile manager closed after terminal rendering error"
                return
            rendered_geometry = self._profile_rendered_geometry
            if rendered_geometry is None:
                self.state.message = "Profile manager closed after terminal error"
                return
            try:
                if screen.getmaxyx() != rendered_geometry:
                    self.state.message = (
                        "Profile manager closed after terminal rendering error"
                    )
                    return
            except curses.error:
                self.state.message = "Profile manager closed after terminal error"
                return
            try:
                key = screen.getch()
            except curses.error:
                self.state.message = "Profile manager closed after terminal error"
                return
            try:
                if screen.getmaxyx() != rendered_geometry:
                    self.state.message = (
                        "Profile manager closed after terminal rendering error"
                    )
                    return
            except curses.error:
                self.state.message = "Profile manager closed after terminal error"
                return

            if pending_delete is not None:
                if key in (ord("y"), ord("Y")):
                    self._delete_profile(pending_delete)
                    pending_delete = None
                    try:
                        names = self.profile_store.list_names()
                        preview_error = ""
                    except ProfileError as exc:
                        names = ()
                        preview_error = f"Profile manager failed: {exc}"
                    preview_name = None
                elif key in (ord("n"), ord("N"), 27):
                    pending_delete = None
                continue

            if key in (27, ord("m"), ord("q")):
                return
            if key == curses.KEY_RESIZE:
                continue
            if key == curses.KEY_UP and names:
                selected = (selected - 1) % len(names)
                preview_name = None
            elif key == curses.KEY_DOWN and names:
                selected = (selected + 1) % len(names)
                preview_name = None
            elif key in (10, 13, curses.KEY_ENTER) and names:
                try:
                    action_layout = self.profile_store.load(names[selected])
                except ProfileError as exc:
                    preview_layout = None
                    preview_error = f"Profile load failed: {exc}"
                    self.state.message = preview_error
                    continue
                self._load_profile(names[selected], layout=action_layout)
                return
            elif key == ord("s"):
                name = self._prompt_profile_name(screen)
                if name:
                    self._save_profile(name)
                    try:
                        names = self.profile_store.list_names()
                        preview_error = ""
                    except ProfileError as exc:
                        names = ()
                        preview_error = f"Profile manager failed: {exc}"
                    preview_name = None
            elif key == ord("n") and names:
                old_name = names[selected]
                new_name = self._prompt_profile_name(screen)
                if new_name:
                    try:
                        self.profile_store.rename(old_name, new_name)
                        names = self.profile_store.list_names()
                        selected = names.index(new_name)
                        preview_error = ""
                        self.state.message = f"Profile renamed: {old_name} → {new_name}"
                    except ProfileError as exc:
                        preview_error = f"Profile rename failed: {exc}"
                        self.state.message = preview_error
                    preview_name = None
            elif key == ord("d") and names:
                pending_delete = names[selected]

    def _confirm_action(self, screen: Any, prompt: str) -> bool:
        blocking_input = False
        try:
            height, width = screen.getmaxyx()
            lines = (prompt, "Y=yes; else=no")
            box_width = max(len(line) for line in lines) + 4
            if height < 5 or width < box_width + 2:
                return False
            y = max(0, (height - 5) // 2)
            x = max(1, (width - box_width) // 2)
            for row in range(5):
                screen.addnstr(
                    y + row,
                    x,
                    " " * box_width,
                    box_width,
                    curses.A_REVERSE,
                )
            for row, line in enumerate(lines):
                attr = curses.A_REVERSE | (curses.A_BOLD if row == 0 else 0)
                screen.addnstr(
                    y + 1 + row,
                    x + 2,
                    line,
                    box_width - 4,
                    attr,
                )
            screen.refresh()
            final_height, final_width = screen.getmaxyx()
            if (final_height, final_width) != (height, width):
                return False
            try:
                screen.timeout(-1)
                blocking_input = True
            except (AttributeError, curses.error):
                pass
            key = screen.getch()
            if screen.getmaxyx() != (final_height, final_width):
                return False
            return key in (ord("y"), ord("Y"))
        except curses.error:
            return False
        finally:
            if blocking_input:
                try:
                    screen.timeout(50)
                except (AttributeError, curses.error):
                    pass

    def _draw_profile_manager(
        self,
        screen: Any,
        names: tuple[str, ...],
        selected: int,
        pending_delete: str | None = None,
        *,
        preview_layout: ColorLayout | None = None,
        preview_error: str = "",
    ) -> bool:
        self._render_failed = False
        self._profile_rendered_geometry = None
        try:
            height, width = screen.getmaxyx()
        except curses.error:
            return False
        self._profile_rendered_geometry = (height, width)
        if height >= 43 and width >= 160:
            try:
                screen.erase()
            except curses.error:
                return False
            composed = compose_profiles(
                self.state,
                names=names,
                selected=selected,
                preview_layout=preview_layout,
                preview_error=preview_error,
                pending_delete=pending_delete,
                power_state=self.power_state,
                version=__version__,
                message=self.state.message,
            )
            return self._draw_composed_screen(screen, composed, height, width)
        if height >= 30 and width >= 100:
            composed = compose_responsive_profiles(
                self.state, width=width, height=height,
                names=names, selected=selected, preview_layout=preview_layout,
                preview_error=preview_error, pending_delete=pending_delete,
                power_state=self.power_state, version=__version__,
                message=self.state.message,
            )
            return self._draw_composed_screen(screen, composed, height, width)
        box_width = min(56, width - 6)
        visible = min(8, max(1, height - 12))
        box_height = visible + 9
        y = max(1, (height - box_height) // 2)
        x = max(2, (width - box_width) // 2)
        rendered = True

        def add(row: int, column: int, text: str, attr: int) -> None:
            nonlocal rendered
            rendered = (
                self._safe_add(
                    screen,
                    row,
                    column,
                    text,
                    attr,
                    require_full=True,
                )
                and rendered
            )

        for row in range(box_height):
            add(y + row, x, " " * box_width, curses.A_REVERSE)
        add(
            y + 1,
            x + 2,
            "LOCAL PROFILES",
            curses.A_REVERSE | curses.A_BOLD,
        )
        add(
            y + 2,
            x + 2,
            "Enter load  ·  S save  ·  D delete  ·  Esc close",
            curses.A_REVERSE,
        )
        if not names:
            add(
                y + 4,
                x + 2,
                "No saved profiles",
                curses.A_REVERSE | curses.A_DIM,
            )
        else:
            start = max(0, min(selected - visible + 1, len(names) - visible))
            for row, name in enumerate(names[start : start + visible]):
                index = start + row
                marker = "▶" if index == selected else " "
                attr = curses.A_REVERSE | (curses.A_BOLD if index == selected else 0)
                add(y + 4 + row, x + 2, f"{marker} {name}", attr)
        if preview_layout is not None and not preview_error:
            for row, zones in enumerate((FIRMWARE_ZONE_ORDER[:2], FIRMWARE_ZONE_ORDER[2:])):
                preview = "  ".join(
                    f"{zone.value.upper()} #{getattr(preview_layout, zone.value)}"
                    for zone in zones
                )
                add(y + box_height - 5 + row, x + 2, preview, curses.A_REVERSE)
        if pending_delete is not None:
            add(
                y + box_height - 3,
                x + 2,
                f"Delete {pending_delete}?",
                curses.A_REVERSE | curses.A_BOLD,
            )
            add(
                y + box_height - 2,
                x + 2,
                "Y confirm · N cancel",
                curses.A_REVERSE | curses.A_BOLD,
            )
        else:
            status_message = preview_error or self.state.message
            add(
                y + box_height - 2,
                x + 2,
                status_message[: max(0, box_width - 4)],
                curses.A_REVERSE | (curses.A_BOLD if preview_error else curses.A_DIM),
            )
        if not rendered:
            return False
        try:
            screen.refresh()
            return True
        except curses.error:
            return False

    def _prompt_profile_name(self, screen: Any) -> str | None:
        prompt = "Profile name (letters, digits, _ or -): "
        blocking_input = False
        try:
            height, width = screen.getmaxyx()
            row = max(1, height - 3)
            if not self._safe_add(
                screen,
                row,
                2,
                " " * max(0, width - 4),
                require_full=True,
            ):
                raise curses.error
            if not self._safe_add(
                screen,
                row,
                2,
                prompt,
                curses.A_BOLD,
                require_full=True,
            ):
                raise curses.error
            screen.refresh()
            if screen.getmaxyx() != (height, width):
                raise curses.error
            curses.echo()
            set_cursor_visibility(1)
            try:
                screen.timeout(-1)
                blocking_input = True
            except (AttributeError, curses.error):
                pass
            raw = screen.getstr(row, 2 + len(prompt), 32).decode("ascii")
            if screen.getmaxyx() != (height, width):
                raise curses.error
            return raw or None
        except (UnicodeDecodeError, curses.error):
            self.state.message = "Profile name entry cancelled"
            return None
        finally:
            if blocking_input:
                try:
                    screen.timeout(50)
                except (AttributeError, curses.error):
                    pass
            try:
                curses.noecho()
            except curses.error:
                pass
            set_cursor_visibility(0)

    def _trigger_effect_input(self, key: int, *, now: float | None = None) -> bool:
        runtime = self.effect_runtime
        if runtime is None or runtime.spec.kind not in (
            EffectKind.REACTIVE,
            EffectKind.RIPPLE,
        ):
            return False
        zone = effect_zone_for_key(key, self.state.selected_zone)
        triggered = runtime.trigger(zone, now=monotonic() if now is None else now)
        if triggered:
            self.state.message = (
                f"{runtime.spec.kind.value} input: {zone.value.upper()} zone"
            )
        return triggered

    def _start_effect(self, *, now: float | None = None) -> bool:
        if self.state.effect_running:
            self.state.message = "Effect is already running"
            return False
        runtime = EffectRuntime(
            backend=self.backend,
            spec=self.state.effect_spec,
            rate_hz=2.0,
        )
        runtime.start(now=monotonic() if now is None else now)
        self.effect_runtime = runtime
        self.state.effect_running = True
        self.state.effect_frame = None
        self.state.effect_frame_uncertain = False
        self.state.effect_restore_pending = False
        self.state.message = f"{self.state.effect_kind.value} effect started at 2 Hz"
        return True

    def _tick_effect(self, *, now: float | None = None, authorized: bool) -> bool:
        runtime = self.effect_runtime
        if runtime is None:
            return False
        try:
            wrote = runtime.tick(
                now=monotonic() if now is None else now,
                authorized=authorized,
            )
        except SysfsError as exc:
            needs_restore = (
                runtime.write_state_uncertain
                or (
                    runtime.last_written is not None
                    and runtime.last_written != runtime.spec.base
                )
            )
            self.state.effect_running = needs_restore
            self.state.effect_restore_pending = needs_restore
            self.state.effect_frame_uncertain = needs_restore
            if needs_restore:
                detail = (
                    "base restoration is pending, but device state is uncertain; "
                    "explicit Stop must verify it before restoration"
                    if runtime.write_state_uncertain
                    else "base restoration is pending"
                )
                self.state.message = f"Effect stopped after firmware error: {exc}; {detail}"
            else:
                self.effect_runtime = None
                self.state.effect_frame = None
                self.state.effect_frame_uncertain = False
                self.state.effect_restore_pending = False
                self.state.message = f"Effect stopped after firmware error: {exc}"
            return False
        if wrote and runtime.last_written is not None:
            self.state.effect_frame = runtime.last_written
            self.state.effect_frame_uncertain = False
            self.state.effect_restore_pending = False
        if not runtime.active and runtime.observed_power_off:
            self.power_state = "off"
            needs_restore = (
                runtime.last_written is not None
                and runtime.last_written != runtime.spec.base
            )
            self.state.effect_running = needs_restore
            self.state.effect_restore_pending = needs_restore
            if needs_restore:
                self.state.message = (
                    "Keyboard power is off; effect base restoration is pending"
                )
            else:
                self.effect_runtime = None
                self.state.effect_frame = None
                self.state.effect_frame_uncertain = False
                self.state.effect_restore_pending = False
                self.state.message = "Effect stopped because keyboard power is off"
        return wrote

    def _stop_effect(self) -> bool:
        runtime = self.effect_runtime
        if runtime is None:
            self.state.effect_running = False
            self.state.effect_frame = None
            self.state.effect_frame_uncertain = False
            self.state.effect_restore_pending = False
            self.state.message = "No effect is running"
            return False
        had_verified_frame = runtime.last_written is not None
        needs_restore = (
            runtime.write_state_uncertain
            or (
                runtime.last_written is not None
                and runtime.last_written != runtime.spec.base
            )
        )
        try:
            restored = runtime.stop()
        except SysfsError as exc:
            self.state.effect_running = needs_restore
            self.state.effect_restore_pending = needs_restore
            self.state.effect_frame_uncertain = needs_restore
            self.state.message = f"Effect base restoration is still pending: {exc}"
            return False
        if runtime.observed_power_off:
            self.power_state = "off"
        elif needs_restore:
            self.power_state = "on"
        self.state.effect_frame_uncertain = runtime.write_state_uncertain
        if not runtime.write_state_uncertain and runtime.last_written is not None:
            self.state.effect_frame = runtime.last_written
        if runtime.write_state_uncertain:
            self.state.effect_running = needs_restore
            self.state.effect_restore_pending = needs_restore
            self.state.message = (
                "Effect device state remains uncertain; restoration is blocked"
            )
            return False
        if runtime.last_written == runtime.spec.base and (
            had_verified_frame or needs_restore
        ):
            self.state.effect_running = False
            self.state.draft = runtime.spec.base
            self.state.mark_applied()
            self.power_state = "on"
            self.state.message = (
                "Effect stopped; base layout restored"
                if restored
                else "Effect stopped; verified base layout active"
            )
            self.effect_runtime = None
            self.state.effect_frame = None
            self.state.effect_frame_uncertain = False
            self.state.effect_restore_pending = False
            return True
        if needs_restore and not restored:
            self.state.effect_running = True
            self.state.effect_restore_pending = True
            if runtime.observed_power_off:
                self.power_state = "off"
                self.state.message = "Effect base restoration is pending while power is off"
            else:
                self.state.message = (
                    "Effect state verified; press S again to restore the base layout"
                )
            return False
        self.state.effect_running = False
        self.state.effect_frame = None
        self.state.effect_frame_uncertain = False
        self.state.effect_restore_pending = False
        self.state.message = "Effect stopped"
        self.effect_runtime = None
        return True

    def _apply(self) -> None:
        if not self.state.dirty:
            self.state.message = "No unapplied changes"
            return
        try:
            self.backend.write_colors(self.state.draft.to_wire())
            self.state.mark_applied()
        except SysfsError as exc:
            self.state.message = f"Apply failed: {exc}"

    def _load_profile(self, name: str, *, layout: ColorLayout | None = None) -> None:
        try:
            self.state.draft = self.profile_store.load(name) if layout is None else layout
            self.state.preset_index = -1
            if self.state.dirty:
                self.state.message = (
                    f"Profile loaded locally: {name} — press A to apply"
                )
            else:
                self.state.message = f"Profile already active: {name}"
        except ProfileError as exc:
            self.state.message = f"Profile load failed: {exc}"

    def _save_profile(self, name: str) -> None:
        try:
            self.profile_store.save(name, self.state.draft)
            self.state.message = f"Saved local profile: {name}"
        except ProfileError as exc:
            self.state.message = f"Profile save failed: {exc}"

    def _delete_profile(self, name: str) -> None:
        try:
            self.profile_store.delete(name)
            self.state.message = f"Deleted local profile: {name}"
        except ProfileError as exc:
            self.state.message = f"Profile delete failed: {exc}"

    def _restore(self) -> None:
        try:
            status = self.backend.status()
            live = ColorLayout.from_wire(",".join(status["colors"]))
            live_original = ColorLayout.from_wire(",".join(status["original"]))
        except (SysfsError, ValidationError) as exc:
            self.state.message = f"Restore failed: {exc}"
            return
        if live == live_original:
            had_dirty_draft = self.state.dirty
            self.state.original = live_original
            self.state.mark_restored()
            if had_dirty_draft:
                self.state.message = (
                    "Original layout already active; local draft discarded"
                )
            else:
                self.state.message = "Original layout is already active"
            return
        try:
            self.backend.restore()
            verified = self.backend.status()
            restored = ColorLayout.from_wire(",".join(verified["colors"]))
            verified_original = ColorLayout.from_wire(",".join(verified["original"]))
            if restored != verified_original:
                raise ValidationError(
                    "restored colors no longer match the saved original"
                )
            self.state.original = verified_original
            self.state.mark_restored()
        except (SysfsError, ValidationError) as exc:
            self.state.message = f"Restore failed: {exc}"

    def _refresh(self) -> None:
        if self.state.dirty:
            self.state.message = "Apply or discard local changes before refreshing"
            return
        try:
            refreshed = self._load_state()
            self.state = refreshed
            self.state.message = "Device status refreshed"
        except SysfsError as exc:
            self.state.message = f"Refresh failed: {exc}"

    def _edit_color_modal(
        self,
        screen: Any,
        geometry: tuple[int, int],
    ) -> None:
        height, width = geometry
        input_text = ""
        error = ""
        blocking_input = False
        set_cursor_visibility(1)
        try:
            try:
                screen.timeout(-1)
                blocking_input = True
            except (AttributeError, curses.error):
                pass
            while True:
                self._render_failed = False
                compose = (
                    compose_exact_hex_modal
                    if (height, width) == (55, 160)
                    else compose_responsive_hex_modal
                )
                options = {} if compose is compose_exact_hex_modal else {
                    "width": width, "height": height,
                }
                composed = compose(
                    self.state, power_state=self.power_state,
                    version=__version__, input_text=input_text, error=error,
                    **options,
                )
                if not self._draw_composed_screen(screen, composed, height, width):
                    raise curses.error
                if screen.getmaxyx() != geometry:
                    raise curses.error
                origin_y = max(0, (height - composed.height) // 2)
                origin_x = max(0, (width - composed.width) // 2)
                modal_x = 43 if compose is compose_exact_hex_modal else (width - 74) // 2
                modal_y = 17 if compose is compose_exact_hex_modal else (height - 18) // 2
                try:
                    screen.move(
                        origin_y + modal_y + 3,
                        origin_x + modal_x + 8 + len(input_text),
                    )
                except (AttributeError, curses.error):
                    pass
                key = screen.getch()
                if screen.getmaxyx() != geometry:
                    raise curses.error
                if key in (27,):
                    self.state.message = "Exact color entry cancelled"
                    return
                if key == curses.KEY_RESIZE:
                    raise curses.error
                if key in (10, 13, curses.KEY_ENTER):
                    if len(input_text) == 6:
                        self.state.set_selected_color(input_text)
                        self.state.message = (
                            f"Local {self.state.selected_zone.value} color set to "
                            f"#{self.state.selected_color}"
                        )
                        return
                    missing = 6 - len(input_text)
                    suffix = "digit" if missing == 1 else "digits"
                    error = f"needs {missing} more {suffix}"
                    continue
                if key == 21:
                    input_text = ""
                    error = ""
                    continue
                if key in (curses.KEY_BACKSPACE, 127, 8):
                    input_text = input_text[:-1]
                    error = ""
                    continue
                if 0 <= key < 256:
                    character = chr(key).upper()
                    if character in "0123456789ABCDEF":
                        if len(input_text) < 6:
                            input_text += character
                            error = ""
                        else:
                            error = "six digits only"
                    elif character.isprintable():
                        error = "use hexadecimal digits 0-9 and A-F"
        except (ValidationError, curses.error, StopIteration):
            self.state.message = (
                "Color entry cancelled after terminal resize or capability error"
            )
        finally:
            if blocking_input:
                try:
                    screen.timeout(50)
                except (AttributeError, curses.error):
                    pass
            set_cursor_visibility(0)

    def _edit_color(self, screen: Any) -> None:
        try:
            geometry = screen.getmaxyx()
        except curses.error:
            self.state.message = "Color entry cancelled after terminal resize or capability error"
            return
        if geometry[0] < 24 or geometry[1] < 78:
            self.state.message = "Terminal too small for color entry"
            return
        self._edit_color_modal(screen, geometry)


def run_tui(
    backend: SysfsBackend | None = None,
    profile_store: ProfileStore | None = None,
) -> None:
    """Load verified device state and start the curses application."""
    CursesTui(backend or SysfsBackend(), profile_store=profile_store).run()
