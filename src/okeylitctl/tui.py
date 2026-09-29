"""Dependency-free curses TUI for safe, deliberate lighting changes."""

from __future__ import annotations

import curses
from enum import Enum, auto
from typing import Any

from . import __version__
from .keyboard_layout import ProjectedKey, project_keyboard
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
            workspace_visible = self._draw(screen)
            rendered_geometry = self._rendered_geometry
            if rendered_geometry is None:
                return
            try:
                if screen.getmaxyx() != rendered_geometry:
                    self.state.message = (
                        "Terminal resized — workspace redrawn before accepting input"
                    )
                    continue
            except curses.error:
                return
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
            if (height, width) != rendered_geometry:
                self.state.message = "Terminal resized — action cancelled; press again"
                continue
            command = handle_key(
                self.state,
                key,
                actions_enabled=(
                    workspace_visible and height >= 24 and width >= 78
                ),
            )
            if command is TuiCommand.QUIT:
                if self.state.dirty:
                    if not self._confirm_action(screen, "Quit?"):
                        self.state.message = "Quit cancelled — local changes preserved"
                        continue
                return
            if command is TuiCommand.APPLY:
                self._apply()
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

        cyan = curses.color_pair(1) if self.colors_enabled else curses.A_BOLD
        chip = curses.color_pair(2) if self.colors_enabled else curses.A_REVERSE
        good = curses.color_pair(3) if self.colors_enabled else curses.A_BOLD
        warn = curses.color_pair(4) if self.colors_enabled else curses.A_BOLD
        top = max(0, (height - 24) // 2)

        self._safe_add(screen, top + 1, 2, "OKEYLITCTL", cyan | curses.A_BOLD)
        self._safe_add(
            screen,
            top + 1,
            14,
            f"v{__version__}  ·  SAFE KEYBOARD LIGHTING",
            curses.A_DIM,
        )
        state_text = f" ● DEVICE {self.power_state.upper()} "
        self._safe_add(
            screen,
            top + 1,
            max(2, width - len(state_text) - 3),
            state_text,
            chip,
        )
        self._safe_add(
            screen,
            top + 2,
            2,
            "PHOTO-MATCHED 100-KEY KEYBOARD",
            curses.A_BOLD,
        )
        dirty = "UNAPPLIED CHANGES" if self.state.dirty else "SYNCHRONIZED"
        self._safe_add(
            screen,
            top + 2,
            width - len(dirty) - 3,
            dirty,
            warn if self.state.dirty else good,
        )
        self._safe_add(
            screen,
            top + 3,
            2,
            "APPROX. FOUR-ZONE SHADING · NOT PER-KEY",
            curses.A_DIM,
            require_full=True,
        )

        self._draw_keyboard_visualization(screen, width, top + 4)

        selected = self.state.selected_zone
        color = self.state.selected_color
        zones = (
            f"1 RIGHT #{self.state.draft.right}  "
            f"2 CENTER #{self.state.draft.center}  "
            f"3 LEFT #{self.state.draft.left}  "
            f"4 WASD #{self.state.draft.wasd}"
        )
        self._safe_add(
            screen,
            top + 12,
            2,
            zones,
            curses.A_BOLD,
            require_full=True,
        )
        current_color = getattr(self.state.current, selected.value)
        editor = (
            f"EDIT {selected.value.upper():<6}  LIVE #{current_color}  "
            f"DRAFT #{color}  ·  LOCAL UNTIL A APPLY"
        )
        self._safe_add(
            screen,
            top + 13,
            2,
            editor,
            cyan | curses.A_BOLD,
            require_full=True,
        )

        channels = ("RED", "GREEN", "BLUE")
        for row, (name, offset) in enumerate(zip(channels, (0, 2, 4))):
            value = int(color[offset : offset + 2], 16)
            active = row == self.state.selected_channel
            attr = cyan | curses.A_BOLD if active else 0
            marker = "▶" if active else " "
            filled = round(value / 255 * 18)
            bar = "━" * filled + "─" * (18 - filled)
            self._safe_add(
                screen,
                top + 14 + row,
                3,
                f"{marker} {name:<5} {bar}  {value:3d}  {value:02X}",
                attr,
                require_full=True,
            )

        preset = "Custom" if self.state.preset_index < 0 else PRESETS[self.state.preset_index][0]
        self._safe_add(
            screen,
            top + 17,
            2,
            f"PRESET {preset}  ·  P CYCLE  ·  E EXACT HEX  ·  X DISCARD DRAFT",
            curses.A_DIM,
            require_full=True,
        )
        self._safe_add(
            screen,
            top + 18,
            2,
            self.state.message,
            good if not self.state.dirty else warn,
        )
        commands_edit = (
            "←/→ ZONE  ↑/↓ RGB  +/- FINE  [/] COARSE  P PRESET  E HEX  X DISCARD"
        )
        commands_global = (
            "A APPLY NOW  M PROFILES  O ORIGINAL  R REFRESH  ? HELP  Q QUIT"
        )
        self._safe_add(
            screen,
            top + 20,
            2,
            commands_edit,
            curses.A_DIM,
            require_full=True,
        )
        self._safe_add(
            screen,
            top + 21,
            2,
            commands_global,
            curses.A_BOLD,
            require_full=True,
        )

        if self.state.help_visible:
            self._draw_help(screen)
        try:
            screen.refresh()
        except curses.error:
            self._render_failed = True
        return not self._render_failed

    @staticmethod
    def _compact_key_legend(item: ProjectedKey, interior: int) -> str:
        legend = item.key.legend
        if len(legend) <= interior:
            return legend
        aliases = {
            "ESC": "E",
            "PWR": "P",
            "DEL": "D",
            "CALC": "C",
            "INS": "I",
            "PRTSC": "PRT",
            "NUMLK": "NUM",
            "BKSP": "BSP",
            "ENTER": "ENT",
            "SHIFT": "SFT",
            "CTRL": "CTL",
        }
        compact = aliases.get(legend, legend)
        if compact.startswith("F") and compact[1:].isdigit():
            number = int(compact[1:])
            if interior == 1:
                compact = str(number) if number < 10 else chr(ord("A") + number - 10)
            else:
                compact = compact[1:]
        return compact[:interior]

    def _key_attr(self, zone: Zone) -> int:
        selected = zone is self.state.selected_zone
        fallback = curses.A_REVERSE if selected else curses.A_DIM
        if not self.colors_enabled or getattr(curses, "COLORS", 0) < 256:
            return fallback
        color = getattr(self.state.draft, zone.value)
        pair_number = 30 + FIRMWARE_ZONE_ORDER.index(zone)
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

    def _draw_projected_key(
        self,
        screen: Any,
        item: ProjectedKey,
        origin_x: int,
        origin_y: int,
        *,
        continuation: bool = False,
    ) -> None:
        if item.width < 3:
            token = item.key.legend[: item.width].center(item.width)
            self._safe_add(
                screen,
                origin_y + item.screen_row,
                origin_x + item.screen_x,
                token,
                self._key_attr(item.zone),
                require_full=True,
            )
            return
        interior = item.width - 2
        legend = self._compact_key_legend(item, interior)
        if continuation:
            token = "│" + legend.center(interior) + "│"
        else:
            token = "[" + legend.center(interior) + "]"
        self._safe_add(
            screen,
            origin_y + item.screen_row,
            origin_x + item.screen_x,
            token,
            self._key_attr(item.zone),
            require_full=True,
        )

    def _draw_keyboard_visualization(
        self, screen: Any, width: int, origin_y: int
    ) -> None:
        projection = project_keyboard(width)
        origin_x = max(1, (width - projection.width) // 2)
        rows: dict[int, list[ProjectedKey]] = {}
        for item in projection.keys:
            rows.setdefault(item.screen_row, []).append(item)
            for continuation_row in self._continuation_rows(item):
                rows.setdefault(continuation_row, []).append(item)
        for screen_row in range(7):
            row_items = rows[6] if screen_row == 5 else rows.get(screen_row, [])
            if not row_items:
                continue
            left = min(item.screen_x for item in row_items) - 1
            right = max(item.screen_x + item.width for item in row_items)
            self._safe_add(
                screen,
                origin_y + screen_row,
                origin_x + left,
                "╱",
                curses.A_DIM,
                require_full=True,
            )
            self._safe_add(
                screen,
                origin_y + screen_row,
                origin_x + right,
                "╲",
                curses.A_DIM,
                require_full=True,
            )
        for item in sorted(
            projection.keys, key=lambda projected: (projected.screen_row, projected.screen_x)
        ):
            self._draw_projected_key(screen, item, origin_x, origin_y)
            for continuation_row in self._continuation_rows(item):
                continuation = ProjectedKey(
                    key=item.key,
                    screen_x=item.screen_x,
                    screen_row=continuation_row,
                    width=item.width,
                    row_span=1,
                    zone=item.zone,
                )
                self._draw_projected_key(
                    screen,
                    continuation,
                    origin_x,
                    origin_y,
                    continuation=True,
                )
        bottom_items = rows[6]
        left = min(item.screen_x for item in bottom_items) - 1
        right = max(item.screen_x + item.width for item in bottom_items)
        base_width = right - left + 1
        self._safe_add(
            screen,
            origin_y + 7,
            origin_x + left,
            "╰" + "═" * (base_width - 2) + "╯",
            curses.A_DIM,
            require_full=True,
        )

    @staticmethod
    def _continuation_rows(item: ProjectedKey) -> tuple[int, ...]:
        if item.row_span != 2:
            return ()
        if item.key.id == "KP_ENTER":
            return (5, 6)
        return (item.screen_row + 1,)

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
            if (final_height, final_width) != (height, width):
                return False
            key = screen.getch()
            if screen.getmaxyx() != (final_height, final_width):
                return False
            return key in (ord("y"), ord("Y"))
        except curses.error:
            return False

    def _draw_profile_manager(
        self,
        screen: Any,
        names: tuple[str, ...],
        selected: int,
        pending_delete: str | None = None,
    ) -> bool:
        self._profile_rendered_geometry = None
        try:
            height, width = screen.getmaxyx()
        except curses.error:
            return False
        self._profile_rendered_geometry = (height, width)
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
            if screen.getmaxyx() != (height, width):
                raise curses.error
            curses.echo()
            set_cursor_visibility(1)
            raw = screen.getstr(row, 2 + len(prompt), 32).decode("ascii")
            if screen.getmaxyx() != (height, width):
                raise curses.error
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

    def _edit_color(self, screen: Any) -> None:
        prompt = f"Enter {self.state.selected_zone.value.upper()} color (RRGGBB): "
        try:
            height, width = screen.getmaxyx()
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
            if screen.getmaxyx() != (height, width):
                raise curses.error
            curses.echo()
            set_cursor_visibility(1)
            raw = screen.getstr(height - 3, 2 + len(prompt), 6).decode("ascii")
            if screen.getmaxyx() != (height, width):
                raise curses.error
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
