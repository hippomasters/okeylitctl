"""Dependency-free curses TUI for safe, deliberate lighting changes."""

from __future__ import annotations

import curses
from enum import Enum, auto
from typing import Any

from . import __version__
from .models import ColorLayout, FIRMWARE_ZONE_ORDER, Zone
from .sysfs import SysfsBackend, SysfsError
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
    QUIT = auto()


def rgb_to_xterm_index(color: str) -> int:
    """Approximate RRGGBB with the xterm 6x6x6 color cube."""
    channels = [int(color[offset : offset + 2], 16) for offset in (0, 2, 4)]
    cube = [round(channel / 255 * 5) for channel in channels]
    return 16 + 36 * cube[0] + 6 * cube[1] + cube[2]


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

    if key in (curses.KEY_RIGHT, ord("l"), 9):
        state.select_next_zone()
    elif key in (curses.KEY_LEFT, ord("h"), curses.KEY_BTAB):
        state.select_previous_zone()
    elif key == curses.KEY_DOWN:
        state.selected_channel = (state.selected_channel + 1) % 3
    elif key == curses.KEY_UP:
        state.selected_channel = (state.selected_channel - 1) % 3
    elif key in (ord("+"), ord("=")):
        state.adjust_selected_channel(1)
    elif key == ord("-"):
        state.adjust_selected_channel(-1)
    elif key == ord("]"):
        state.adjust_selected_channel(16)
    elif key == ord("["):
        state.adjust_selected_channel(-16)
    elif key in (ord("1"), ord("2"), ord("3"), ord("4")):
        state.selected_zone = FIRMWARE_ZONE_ORDER[key - ord("1")]
    elif key == ord("p"):
        state.preset_index = (state.preset_index + 1) % len(PRESETS)
        name, layout = PRESETS[state.preset_index]
        state.draft = layout
        state.message = f"Preset loaded locally: {name} — press A to apply"
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
    elif key == ord("q"):
        return TuiCommand.QUIT
    return TuiCommand.NONE


class CursesTui:
    """Interactive adapter; all editing remains local until Apply."""

    def __init__(self, backend: SysfsBackend):
        self.backend = backend
        self.power_state = "unknown"
        self.colors_enabled = False
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

    def _main(self, screen: Any) -> None:
        screen.keypad(True)
        set_cursor_visibility(0)
        self.colors_enabled = initialize_colors()

        while True:
            self._draw(screen)
            key = screen.getch()
            if key == curses.KEY_RESIZE:
                continue
            height, width = screen.getmaxyx()
            command = handle_key(
                self.state,
                key,
                actions_enabled=height >= 24 and width >= 78,
            )
            if command is TuiCommand.QUIT:
                return
            if command is TuiCommand.APPLY:
                self._apply()
            elif command is TuiCommand.RESTORE:
                self._restore()
            elif command is TuiCommand.REFRESH:
                self._refresh()
            elif command is TuiCommand.EDIT:
                self._edit_color(screen)

    @staticmethod
    def _safe_add(screen: Any, y: int, x: int, text: str, attr: int = 0) -> None:
        height, width = screen.getmaxyx()
        if y < 0 or y >= height or x < 0 or x >= width:
            return
        try:
            screen.addnstr(y, x, text, max(0, width - x - 1), attr)
        except curses.error:
            pass

    def _draw(self, screen: Any) -> None:
        screen.erase()
        height, width = screen.getmaxyx()
        if height < 24 or width < 78:
            self._safe_add(screen, 1, 2, "OKeyLitCtl needs at least 78 x 24", curses.A_BOLD)
            self._safe_add(screen, 3, 2, f"Current terminal: {width} x {height}")
            self._safe_add(screen, 5, 2, "Resize the terminal or press Q to quit.")
            screen.refresh()
            return

        cyan = curses.color_pair(1) if self.colors_enabled else curses.A_BOLD
        chip = curses.color_pair(2) if self.colors_enabled else curses.A_REVERSE
        good = curses.color_pair(3) if self.colors_enabled else curses.A_BOLD
        warn = curses.color_pair(4) if self.colors_enabled else curses.A_BOLD

        self._safe_add(screen, 1, 2, "OKEYLITCTL", cyan | curses.A_BOLD)
        self._safe_add(screen, 1, 14, f"v{__version__}  ·  SAFE KEYBOARD LIGHTING", curses.A_DIM)
        state_text = f" ● DEVICE {self.power_state.upper()} "
        self._safe_add(screen, 1, max(2, width - len(state_text) - 3), state_text, chip)
        self._safe_add(screen, 3, 2, "FOUR-ZONE WORKSPACE", curses.A_BOLD)
        dirty = "UNAPPLIED CHANGES" if self.state.dirty else "SYNCHRONIZED"
        self._safe_add(screen, 3, width - len(dirty) - 3, dirty, warn if self.state.dirty else good)

        card_gap = 2
        card_width = max(16, (width - 4 - card_gap * 3) // 4)
        for index, zone in enumerate(FIRMWARE_ZONE_ORDER):
            x = 2 + index * (card_width + card_gap)
            self._draw_zone_card(screen, 5, x, card_width, zone, index + 1)

        selected = self.state.selected_zone
        color = self.state.selected_color
        self._safe_add(screen, 12, 2, f"EDITING  {selected.value.upper()}", cyan | curses.A_BOLD)
        self._safe_add(screen, 12, 24, f"#{color}", curses.A_BOLD)
        self._safe_add(screen, 12, 35, "Local draft only — A applies the complete layout", curses.A_DIM)

        channels = ("RED", "GREEN", "BLUE")
        for row, (name, offset) in enumerate(zip(channels, (0, 2, 4))):
            value = int(color[offset : offset + 2], 16)
            active = row == self.state.selected_channel
            attr = cyan | curses.A_BOLD if active else 0
            marker = "▶" if active else " "
            filled = round(value / 255 * 24)
            bar = "━" * filled + "─" * (24 - filled)
            self._safe_add(screen, 14 + row * 2, 3, f"{marker} {name:<5}", attr)
            self._safe_add(screen, 14 + row * 2, 13, bar, attr)
            self._safe_add(screen, 14 + row * 2, 39, f"{value:3d}  {value:02X}", attr)

        preset = "Custom" if self.state.preset_index < 0 else PRESETS[self.state.preset_index][0]
        self._safe_add(screen, 20, 2, f"PRESET  {preset}", curses.A_BOLD)
        self._safe_add(screen, 20, 24, "P cycles presets  ·  E enters an exact hex color", curses.A_DIM)
        self._safe_add(screen, height - 3, 2, self.state.message, good if not self.state.dirty else warn)
        footer = "←/→ zone   ↑/↓ channel   +/- fine   [/ ] coarse   A apply   O original   R refresh   ? help   Q quit"
        self._safe_add(screen, height - 1, 1, footer, curses.A_REVERSE)

        if self.state.help_visible:
            self._draw_help(screen)
        screen.refresh()

    def _draw_zone_card(
        self, screen: Any, y: int, x: int, width: int, zone: Zone, number: int
    ) -> None:
        selected = zone is self.state.selected_zone
        color = getattr(self.state.draft, zone.value)
        border_attr = curses.color_pair(1) | curses.A_BOLD if selected and self.colors_enabled else (curses.A_BOLD if selected else curses.A_DIM)
        top = "╭" + "─" * (width - 2) + "╮"
        bottom = "╰" + "─" * (width - 2) + "╯"
        self._safe_add(screen, y, x, top, border_attr)
        self._safe_add(screen, y + 1, x, "│" + " " * (width - 2) + "│", border_attr)
        self._safe_add(screen, y + 2, x, "│" + " " * (width - 2) + "│", border_attr)
        self._safe_add(screen, y + 3, x, "│" + " " * (width - 2) + "│", border_attr)
        self._safe_add(screen, y + 4, x, bottom, border_attr)
        self._safe_add(screen, y + 1, x + 2, f"{number}  {zone.value.upper()}", curses.A_BOLD)
        self._safe_add(screen, y + 3, x + 2, f"#{color}", curses.A_BOLD)
        if self.colors_enabled and getattr(curses, "COLORS", 0) >= 256:
            pair_number = 20 + number
            background = rgb_to_xterm_index(color)
            luminance = sum(int(color[i : i + 2], 16) * weight for i, weight in zip((0, 2, 4), (299, 587, 114))) / 1000
            foreground = 16 if luminance > 145 else 231
            try:
                curses.init_pair(pair_number, foreground, background)
                swatch = " " * max(4, width - 13)
                self._safe_add(screen, y + 3, x + width - len(swatch) - 2, swatch, curses.color_pair(pair_number))
            except curses.error:
                pass

    def _draw_help(self, screen: Any) -> None:
        height, width = screen.getmaxyx()
        lines = (
            "OKEYLITCTL HELP",
            "",
            "1–4 / ← →    Select a named keyboard zone",
            "↑ ↓           Select red, green, or blue channel",
            "+ -           Adjust by one    [ ] adjust by sixteen",
            "E             Enter an exact six-digit hex color",
            "P             Cycle curated local presets",
            "A             Apply and verify the complete four-zone layout",
            "O             Restore the module's original layout",
            "X             Discard unapplied local edits",
            "R             Refresh from the device (blocked while dirty)",
            "Q             Quit without applying local edits",
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

    def _apply(self) -> None:
        if not self.state.dirty:
            self.state.message = "No unapplied changes"
            return
        try:
            self.backend.write_colors(self.state.draft.to_wire())
            self.state.mark_applied()
        except SysfsError as exc:
            self.state.message = f"Apply failed: {exc}"

    def _restore(self) -> None:
        if self.state.current == self.state.original and not self.state.dirty:
            self.state.message = "Original layout is already active"
            return
        try:
            self.backend.restore()
            self.state.mark_restored()
        except SysfsError as exc:
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

    def _edit_color(self, screen: Any) -> None:
        height, _ = screen.getmaxyx()
        prompt = f"Enter {self.state.selected_zone.value.upper()} color (RRGGBB): "
        self._safe_add(screen, height - 3, 2, " " * 70)
        self._safe_add(screen, height - 3, 2, prompt, curses.A_BOLD)
        try:
            screen.refresh()
            curses.echo()
            set_cursor_visibility(1)
            raw = screen.getstr(height - 3, 2 + len(prompt), 6).decode("ascii")
            self.state.set_selected_color(raw)
            self.state.message = f"Local {self.state.selected_zone.value} color set to #{self.state.selected_color}"
        except (UnicodeDecodeError, ValidationError):
            self.state.message = "Invalid color — enter exactly six hexadecimal digits"
        except curses.error:
            self.state.message = "Color entry cancelled after terminal resize or capability error"
        finally:
            try:
                curses.noecho()
            except curses.error:
                pass
            set_cursor_visibility(0)


def run_tui(backend: SysfsBackend | None = None) -> None:
    """Load verified device state and start the curses application."""
    CursesTui(backend or SysfsBackend()).run()
