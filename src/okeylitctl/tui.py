"""Dependency-free curses TUI for safe, deliberate lighting changes."""

from __future__ import annotations

import curses
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any

from . import __version__
from .models import ColorLayout, FIRMWARE_ZONE_ORDER, Zone
from .profiles import ProfileError, ProfileStore
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
    PROFILES = auto()
    QUIT = auto()


@dataclass(frozen=True)
class VisualRegion:
    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True)
class WorkspaceFrame:
    x: int
    y: int
    width: int


def workspace_frame(terminal_height: int, terminal_width: int) -> WorkspaceFrame:
    """Center a compact 24-row workspace and cap overly wide layouts."""
    width = min(132, terminal_width)
    x = max(0, (terminal_width - width) // 2)
    y = max(0, (terminal_height - 24) // 2)
    return WorkspaceFrame(x=x, y=y, width=width)


def keyboard_region_geometry(
    terminal_width: int, *, top: int = 0
) -> dict[Zone, VisualRegion]:
    """Return a coarse four-region keyboard silhouette, never per-key geometry."""
    frame = workspace_frame(24, terminal_width)
    margin = frame.x + 2
    gap = 2
    available = frame.width - 4 - gap * 2
    left_width = max(18, available * 24 // 100)
    center_width = max(22, available * 31 // 100)
    right_width = available - left_width - center_width
    if right_width < 26:
        shortage = 26 - right_width
        center_width -= shortage
        right_width = 26

    left = VisualRegion(margin, top + 5, left_width, 6)
    center = VisualRegion(left.x + left.width + gap, top + 5, center_width, 6)
    right = VisualRegion(center.x + center.width + gap, top + 5, right_width, 6)
    wasd = VisualRegion(left.x + 2, top + 7, left.width - 4, 2)
    return {
        Zone.LEFT: left,
        Zone.CENTER: center,
        Zone.RIGHT: right,
        Zone.WASD: wasd,
    }


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
    elif key == ord("m"):
        return TuiCommand.PROFILES
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
        try:
            screen.keypad(True)
        except curses.error:
            return
        set_cursor_visibility(0)
        self.colors_enabled = initialize_colors()

        while True:
            self._draw(screen)
            try:
                key = screen.getch()
            except curses.error:
                return
            if key == curses.KEY_RESIZE:
                continue
            try:
                height, width = screen.getmaxyx()
            except curses.error:
                return
            command = handle_key(
                self.state,
                key,
                actions_enabled=height >= 24 and width >= 78,
            )
            if command is TuiCommand.QUIT:
                if self.state.dirty:
                    if height < 5 or width < 20:
                        return
                    if not self._confirm_action(screen, "Quit?"):
                        self.state.message = "Quit cancelled — local changes preserved"
                        continue
                return
            if command is TuiCommand.APPLY:
                self._apply()
            elif command is TuiCommand.RESTORE:
                restore_needed = self.state.current != self.state.original or self.state.dirty
                if not restore_needed or self._confirm_action(
                    screen, "Restore the original module layout?"
                ):
                    self._restore()
                else:
                    self.state.message = "Restore cancelled"
            elif command is TuiCommand.REFRESH:
                self._refresh()
            elif command is TuiCommand.EDIT:
                self._edit_color(screen)
            elif command is TuiCommand.PROFILES:
                self._profile_manager(screen)

    @staticmethod
    def _safe_add(
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
                return False
            if require_full and len(text) > available:
                return False
            screen.addnstr(y, x, text, available, attr)
            return True
        except curses.error:
            return False

    def _draw(self, screen: Any) -> None:
        try:
            screen.erase()
            height, width = screen.getmaxyx()
        except curses.error:
            return
        if height < 24 or width < 78:
            self._safe_add(screen, 1, 2, "OKeyLitCtl needs at least 78 x 24", curses.A_BOLD)
            self._safe_add(screen, 3, 2, f"Current terminal: {width} x {height}")
            self._safe_add(screen, 5, 2, "Resize the terminal or press Q to quit.")
            try:
                screen.refresh()
            except curses.error:
                pass
            return

        frame = workspace_frame(height, width)
        base = frame.y
        left = frame.x + 2
        right = frame.x + frame.width - 3
        content_width = frame.width - 4

        cyan = curses.color_pair(1) if self.colors_enabled else curses.A_BOLD
        good = curses.color_pair(3) if self.colors_enabled else curses.A_BOLD
        warn = curses.color_pair(4) if self.colors_enabled else curses.A_BOLD

        self._safe_add(screen, base, left, "OKEYLITCTL", cyan | curses.A_BOLD)
        self._safe_add(
            screen,
            base,
            left + 12,
            f"v{__version__}  ·  FOUR-ZONE LIGHTING",
            curses.A_DIM,
        )
        state_text = f"● DEVICE {self.power_state.upper()}"
        draft_text = "* MODIFIED" if self.state.dirty else "SYNCED"
        header_state = f"{state_text}  {draft_text}"
        self._safe_add(
            screen,
            base,
            right - len(header_state) + 1,
            header_state,
            warn if self.state.dirty else good,
        )
        self._safe_add(screen, base + 1, left, "─" * content_width, curses.A_DIM)
        self._safe_add(screen, base + 2, left, "FOUR-ZONE OUTPUT", curses.A_BOLD)
        region_note = (
            "COARSE ZONES · NOT PER-KEY"
            if frame.width < 100
            else "COARSE HARDWARE REGIONS · NOT PER-KEY"
        )
        self._safe_add(
            screen,
            base + 2,
            right - len(region_note) + 1,
            region_note,
            curses.A_DIM,
        )

        self._draw_keyboard_visualization(screen, width, top=base)
        self._draw_editor_panel(screen, frame, cyan, warn)

        status_prefix = "*" if self.state.dirty else "✓"
        self._safe_add(
            screen,
            base + 21,
            left,
            f"{status_prefix} {self.state.message}",
            warn if self.state.dirty else good,
        )
        edit_footer = "←/→ ZONE  ↑/↓ CHANNEL  -/+ FINE  [/] COARSE  [P] PRESET  [E] HEX"
        global_footer = "[A] APPLY  [M] PROFILES  [O] ORIGINAL  [R] REFRESH  [?] HELP  [Q] QUIT"
        self._safe_add(screen, base + 22, left, edit_footer, curses.A_DIM)
        self._safe_add(screen, base + 23, left, global_footer, curses.A_BOLD)

        if self.state.help_visible:
            self._draw_help(screen)
        try:
            screen.refresh()
        except curses.error:
            pass

    def _draw_editor_panel(
        self,
        screen: Any,
        frame: WorkspaceFrame,
        accent: int,
        warning: int,
    ) -> None:
        x = frame.x + 2
        y = frame.y + 12
        width = frame.width - 4
        selected = self.state.selected_zone
        draft = self.state.selected_color
        live = getattr(self.state.current, selected.value)
        dirty = draft != live
        border_attr = accent if dirty else curses.A_DIM
        interior_right = x + width - 2

        def panel_add(row: int, column: int, text: str, attr: int = 0) -> None:
            available = max(0, interior_right - column)
            self._safe_add(screen, row, column, text[:available], attr)

        self._safe_add(screen, y, x, "╭" + "─" * (width - 2) + "╮", border_attr)
        for row in range(1, 8):
            self._safe_add(
                screen,
                y + row,
                x,
                "│" + " " * (width - 2) + "│",
                border_attr,
            )
        self._safe_add(
            screen,
            y + 8,
            x,
            "╰" + "─" * (width - 2) + "╯",
            border_attr,
        )

        zone_name = selected.value.upper()
        if selected is Zone.RIGHT:
            zone_name = "RIGHT / ARROWS / NUMPAD"
        title = f" COLOR EDITOR  ·  ZONE {FIRMWARE_ZONE_ORDER.index(selected) + 1}  {zone_name} "
        state_label = "* UNAPPLIED" if dirty else "SYNCED"
        state_x = x + width - len(state_label) - 3
        title_available = max(0, state_x - (x + 2) - 1)
        self._safe_add(
            screen,
            y,
            x + 2,
            title[:title_available],
            accent | curses.A_BOLD,
        )
        self._safe_add(
            screen,
            y,
            state_x,
            f" {state_label} ",
            warning if dirty else curses.A_BOLD,
        )

        panel_add(y + 2, x + 3, f"DRAFT #{draft}", curses.A_BOLD)
        panel_add(y + 2, x + 23, f"LIVE  #{live}", curses.A_DIM)
        if width < 100:
            action_hint = "A applies immediately" if dirty else "Draft matches device"
        else:
            action_hint = (
                "A applies all four zones immediately"
                if dirty
                else "Device and draft match"
            )
        panel_add(y + 2, x + 43, action_hint, curses.A_DIM)

        channels = ("RED", "GREEN", "BLUE")
        track_width = min(28, max(12, width - 35))
        for row, (name, offset) in enumerate(zip(channels, (0, 2, 4))):
            value = int(draft[offset : offset + 2], 16)
            active = row == self.state.selected_channel
            attr = accent | curses.A_BOLD if active else 0
            marker = "▶" if active else " "
            filled = round(value / 255 * track_width)
            bar = "█" * filled + "░" * (track_width - filled)
            panel_add(
                y + 4 + row,
                x + 3,
                f"{marker} {name:<5} {value:3d}  {value:02X}  [{bar}]",
                attr,
            )

        preset = "Custom" if self.state.preset_index < 0 else PRESETS[self.state.preset_index][0]
        panel_add(y + 7, x + 3, f"PRESET  {preset}", curses.A_BOLD)
        preset_help = (
            "[P] preset  [E] hex  [X] discard"
            if width < 100
            else "[P] cycle preset   [E] exact hex   [X] discard draft"
        )
        panel_add(
            y + 7,
            x + 25,
            preset_help,
            curses.A_DIM,
        )

    def _draw_keyboard_visualization(
        self, screen: Any, width: int, *, top: int = 0
    ) -> None:
        regions = keyboard_region_geometry(width, top=top)
        labels = {
            Zone.LEFT: (
                "3 LEFT" if regions[Zone.LEFT].width < 22 else "3 · LEFT REGION"
            ),
            Zone.CENTER: (
                "2 CENTER"
                if regions[Zone.CENTER].width < 25
                else "2 · CENTER REGION"
            ),
            Zone.RIGHT: (
                "1 RIGHT+ARROWS+NUMPAD"
                if regions[Zone.RIGHT].width < 36
                else "1 · RIGHT + ARROWS + NUMPAD"
            ),
            Zone.WASD: "WASD REGION",
        }
        for zone in (Zone.LEFT, Zone.CENTER, Zone.RIGHT):
            self._draw_visual_region(screen, regions[zone], zone, labels[zone])
        self._draw_visual_region(
            screen,
            regions[Zone.WASD],
            Zone.WASD,
            labels[Zone.WASD],
            compact=True,
        )

    def _draw_visual_region(
        self,
        screen: Any,
        region: VisualRegion,
        zone: Zone,
        label: str,
        *,
        compact: bool = False,
    ) -> None:
        selected = zone is self.state.selected_zone
        color = getattr(self.state.draft, zone.value)
        live = getattr(self.state.current, zone.value)
        dirty = color != live
        inner_width = max(1, region.width - (0 if compact else 4))

        def fit(text: str) -> str:
            return text[:inner_width]
        if selected and self.colors_enabled:
            border_attr = curses.color_pair(1) | curses.A_BOLD
        else:
            border_attr = curses.A_BOLD if selected else curses.A_DIM

        if compact:
            compact_attr = curses.A_REVERSE if selected else curses.A_BOLD
            self._safe_add(
                screen,
                region.y,
                region.x,
                fit(
                    f"{'>' if selected else ''}WASD REGION 4"
                    f"{'*' if dirty else ''}"
                ),
                compact_attr,
            )
            self._safe_add(
                screen,
                region.y + 1,
                region.x,
                fit(f"#{color}{' DRAFT' if dirty else ' SYNCED'}"),
                compact_attr,
            )
            if self.colors_enabled and getattr(curses, "COLORS", 0) >= 256:
                background = rgb_to_xterm_index(color)
                try:
                    curses.init_pair(33, 231, background)
                    swatch_width = max(3, region.width - 9)
                    self._safe_add(
                        screen,
                        region.y + 1,
                        region.x + region.width - swatch_width,
                        " " * swatch_width,
                        curses.color_pair(33),
                    )
                except curses.error:
                    pass
            return

        top = "╭" + "─" * (region.width - 2) + "╮"
        bottom = "╰" + "─" * (region.width - 2) + "╯"
        self._safe_add(screen, region.y, region.x, top, border_attr)
        for row in range(1, region.height - 1):
            self._safe_add(
                screen,
                region.y + row,
                region.x,
                "│" + " " * (region.width - 2) + "│",
                border_attr,
            )
        self._safe_add(
            screen,
            region.y + region.height - 1,
            region.x,
            bottom,
            border_attr,
        )

        title = f"{'▶' if selected else ' '} {label}{' *' if dirty else ''}"
        self._safe_add(
            screen,
            region.y + 1,
            region.x + 2,
            fit(title),
            curses.A_BOLD,
        )

        if zone is not Zone.LEFT:
            live_text = f"LIVE #{live}" if inner_width >= 13 else f"L #{live}"
            if dirty:
                draft_text = (
                    f"DRAFT #{color} *" if inner_width >= 15 else f"D #{color} *"
                )
            else:
                draft_text = (
                    f"DRAFT #{color} SYNCED"
                    if inner_width >= 21
                    else f"D #{color} OK"
                )
            self._safe_add(
                screen,
                region.y + 2,
                region.x + 2,
                fit(live_text),
                curses.A_DIM,
            )
            self._safe_add(
                screen,
                region.y + 3,
                region.x + 2,
                fit(draft_text),
                curses.A_BOLD if dirty else curses.A_DIM,
            )

        self._safe_add(
            screen,
            region.y + region.height - 2,
            region.x + 2,
            fit(
                (
                    f"D #{color} *"
                    if dirty and inner_width < 22
                    else f"LIVE #{live} → DRAFT #{color}{' *' if dirty else ''}"
                )
                if zone is Zone.LEFT
                else f"#{color}"
            ),
            curses.A_BOLD,
        )
        if self.colors_enabled and getattr(curses, "COLORS", 0) >= 256:
            pair_number = 30 + FIRMWARE_ZONE_ORDER.index(zone)
            background = rgb_to_xterm_index(color)
            luminance = sum(
                int(color[offset : offset + 2], 16) * weight
                for offset, weight in zip((0, 2, 4), (299, 587, 114))
            ) / 1000
            foreground = 16 if luminance > 145 else 231
            try:
                curses.init_pair(pair_number, foreground, background)
                swatch_width = max(3, region.width - 13)
                self._safe_add(
                    screen,
                    region.y + region.height - 2,
                    region.x + region.width - swatch_width - 2,
                    " " * swatch_width,
                    curses.color_pair(pair_number),
                )
            except curses.error:
                pass

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
            "+ -           Adjust by one    [ ] adjust by sixteen",
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
        while True:
            try:
                names = self.profile_store.list_names()
            except ProfileError as exc:
                self.state.message = f"Profile manager failed: {exc}"
                return

            if names:
                selected = min(selected, len(names) - 1)
            else:
                selected = 0
            if not self._draw_profile_manager(
                screen, names, selected, pending_delete
            ):
                self.state.message = "Profile manager closed after terminal rendering error"
                return
            try:
                key = screen.getch()
            except curses.error:
                self.state.message = "Profile manager closed after terminal error"
                return

            if pending_delete is not None:
                if key in (ord("y"), ord("Y")):
                    self._delete_profile(pending_delete)
                    pending_delete = None
                elif key in (ord("n"), ord("N"), 27):
                    pending_delete = None
                continue

            if key in (27, ord("m"), ord("q")):
                return
            if key == curses.KEY_RESIZE:
                continue
            if key == curses.KEY_UP and names:
                selected = (selected - 1) % len(names)
            elif key == curses.KEY_DOWN and names:
                selected = (selected + 1) % len(names)
            elif key in (10, 13, curses.KEY_ENTER) and names:
                self._load_profile(names[selected])
                return
            elif key == ord("s"):
                name = self._prompt_profile_name(screen)
                if name:
                    self._save_profile(name)
            elif key == ord("d") and names:
                pending_delete = names[selected]

    def _confirm_action(self, screen: Any, prompt: str) -> bool:
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
            if final_height < 5 or final_width < box_width + 2:
                return False
            return screen.getch() in (ord("y"), ord("Y"))
        except curses.error:
            return False

    def _draw_profile_manager(
        self,
        screen: Any,
        names: tuple[str, ...],
        selected: int,
        pending_delete: str | None = None,
    ) -> bool:
        try:
            height, width = screen.getmaxyx()
        except curses.error:
            return False
        box_width = min(56, width - 6)
        visible = min(10, max(1, height - 10))
        box_height = visible + 7
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
            add(
                y + box_height - 2,
                x + 2,
                self.state.message,
                curses.A_REVERSE | curses.A_DIM,
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
            curses.echo()
            set_cursor_visibility(1)
            raw = screen.getstr(row, 2 + len(prompt), 32).decode("ascii")
            return raw or None
        except (UnicodeDecodeError, curses.error):
            self.state.message = "Profile name entry cancelled"
            return None
        finally:
            try:
                curses.noecho()
            except curses.error:
                pass
            set_cursor_visibility(0)

    def _apply(self) -> None:
        if not self.state.dirty:
            self.state.message = "No unapplied changes"
            return
        try:
            self.backend.write_colors(self.state.draft.to_wire())
            self.state.mark_applied()
        except SysfsError as exc:
            self.state.message = f"Apply failed: {exc}"

    def _load_profile(self, name: str) -> None:
        try:
            self.state.draft = self.profile_store.load(name)
            self.state.preset_index = -1
            self.state.message = f"Profile loaded locally: {name} — press A to apply"
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
        prompt = f"Enter {self.state.selected_zone.value.upper()} color (RRGGBB): "
        try:
            height, _ = screen.getmaxyx()
            if not self._safe_add(
                screen,
                height - 3,
                2,
                " " * 70,
                require_full=True,
            ):
                raise curses.error
            if not self._safe_add(
                screen,
                height - 3,
                2,
                prompt,
                curses.A_BOLD,
                require_full=True,
            ):
                raise curses.error
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


def run_tui(
    backend: SysfsBackend | None = None,
    profile_store: ProfileStore | None = None,
) -> None:
    """Load verified device state and start the curses application."""
    CursesTui(backend or SysfsBackend(), profile_store=profile_store).run()
