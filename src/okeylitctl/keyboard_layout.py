"""Photo-derived HP OMEN 16-wf0xxx keyboard geometry for the TUI.

Key geometry is informational.  Lighting remains four fixed firmware zones and
no key in this module is independently controllable.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil

from .models import Zone


Y_FN = 0.00
Y_NUMBER = 1.08
Y_Q = 2.18
Y_HOME = 3.28
Y_SHIFT = 4.38
Y_BOTTOM = 5.48


@dataclass(frozen=True)
class KeyboardKey:
    id: str
    legend: str
    x: float
    y: float
    width: float
    height: float
    row: str
    secondary: str | None = None


@dataclass(frozen=True)
class ProjectedKey:
    key: KeyboardKey
    screen_x: int
    screen_row: int
    width: int
    row_span: int
    zone: Zone


@dataclass(frozen=True)
class KeyboardProjection:
    unit_width: int
    width: int
    row_origins: dict[str, int]
    keys: tuple[ProjectedKey, ...]


@dataclass(frozen=True)
class BlockProjectedKey:
    key: KeyboardKey
    x: int
    y: int
    width: int
    height: int
    zone: Zone

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height


@dataclass(frozen=True)
class BlockKeyboardProjection:
    width: int
    height: int
    unit_x: int
    unit_y: int
    used_width: int
    keys: tuple[BlockProjectedKey, ...]


@dataclass(frozen=True)
class BlockKeyboardBuffer:
    width: int
    height: int
    lines: tuple[str, ...]
    key_ids: tuple[tuple[str | None, ...], ...]
    zones: tuple[tuple[Zone | None, ...], ...]
    selected: tuple[tuple[bool, ...], ...]


def _key(
    row: str,
    key_id: str,
    legend: str,
    x: float,
    y: float,
    width: float = 1.0,
    height: float = 1.0,
    secondary: str | None = None,
) -> KeyboardKey:
    return KeyboardKey(key_id, legend, x, y, width, height, row, secondary)


FUNCTION_ROW = (
    _key("function", "ESC", "ESC", 0.15, Y_FN, height=0.78),
    _key("function", "F1", "F1", 1.15, Y_FN, height=0.78, secondary="DISPLAY"),
    _key("function", "F2", "F2", 2.15, Y_FN, height=0.78, secondary="BRIGHT-"),
    _key("function", "F3", "F3", 3.15, Y_FN, height=0.78, secondary="BRIGHT+"),
    _key("function", "F4", "F4", 4.15, Y_FN, height=0.78, secondary="KBD LIGHT"),
    _key("function", "F5", "F5", 5.15, Y_FN, height=0.78, secondary="MUTE"),
    _key("function", "F6", "F6", 6.15, Y_FN, height=0.78, secondary="VOL-"),
    _key("function", "F7", "F7", 7.15, Y_FN, height=0.78, secondary="VOL+"),
    _key("function", "F8", "F8", 8.15, Y_FN, height=0.78, secondary="PREV"),
    _key("function", "F9", "F9", 9.15, Y_FN, height=0.78, secondary="PLAY"),
    _key("function", "F10", "F10", 10.15, Y_FN, height=0.78, secondary="NEXT"),
    _key("function", "F11", "F11", 11.15, Y_FN, height=0.78, secondary="CAM?"),
    _key("function", "F12", "F12", 12.15, Y_FN, height=0.78, secondary="WIN LOCK"),
    _key("function", "POWER", "PWR", 13.15, Y_FN, height=0.78, secondary="POWER"),
    _key("function", "DELETE", "DEL", 14.15, Y_FN, height=0.78),
    _key("function", "OMEN", "<>", 15.15, Y_FN, height=0.78, secondary="OMEN"),
    _key("function", "CALCULATOR", "CALC", 16.15, Y_FN, height=0.78),
    _key("function", "INSERT", "INS", 17.15, Y_FN, height=0.78),
    _key("function", "PRINT", "PRTSC", 18.15, Y_FN, height=0.78),
)

NUMBER_ROW = (
    _key("number", "GRAVE", "`", 0.00, Y_NUMBER, secondary="~"),
    _key("number", "DIGIT1", "1", 1.00, Y_NUMBER, secondary="!"),
    _key("number", "DIGIT2", "2", 2.00, Y_NUMBER, secondary="@"),
    _key("number", "DIGIT3", "3", 3.00, Y_NUMBER, secondary="#"),
    _key("number", "DIGIT4", "4", 4.00, Y_NUMBER, secondary="$"),
    _key("number", "DIGIT5", "5", 5.00, Y_NUMBER, secondary="%"),
    _key("number", "DIGIT6", "6", 6.00, Y_NUMBER, secondary="^"),
    _key("number", "DIGIT7", "7", 7.00, Y_NUMBER, secondary="&"),
    _key("number", "DIGIT8", "8", 8.00, Y_NUMBER, secondary="*"),
    _key("number", "DIGIT9", "9", 9.00, Y_NUMBER, secondary="("),
    _key("number", "DIGIT0", "0", 10.00, Y_NUMBER, secondary=")"),
    _key("number", "MINUS", "-", 11.00, Y_NUMBER, secondary="_"),
    _key("number", "EQUAL", "=", 12.00, Y_NUMBER, secondary="+"),
    _key("number", "BACKSPACE", "BKSP", 13.00, Y_NUMBER, width=2.00),
    _key("number", "NUMLOCK", "NUMLK", 15.30, Y_NUMBER),
    _key("number", "KP_DIV", "/", 16.30, Y_NUMBER),
    _key("number", "KP_MUL", "*", 17.30, Y_NUMBER),
    _key("number", "KP_SUB", "-", 18.30, Y_NUMBER),
)

QWERTY_ROW = (
    _key("qwerty", "TAB", "TAB", 0.00, Y_Q, width=1.50, secondary="<->"),
    *(_key("qwerty", letter, letter, 1.50 + index, Y_Q) for index, letter in enumerate("QWERTYUIOP")),
    _key("qwerty", "LBRACKET", "[", 11.50, Y_Q, secondary="{"),
    _key("qwerty", "RBRACKET", "]", 12.50, Y_Q, secondary="}"),
    _key("qwerty", "BACKSLASH", "\\", 13.50, Y_Q, width=1.50, secondary="|"),
    _key("qwerty", "KP7", "7", 15.30, Y_Q, secondary="HOME"),
    _key("qwerty", "KP8", "8", 16.30, Y_Q, secondary="^"),
    _key("qwerty", "KP9", "9", 17.30, Y_Q, secondary="PGUP"),
    _key("qwerty", "KP_ADD", "+", 18.30, Y_Q, height=2.10),
)

HOME_ROW = (
    _key("home", "CAPSLOCK", "CAPS", 0.00, Y_HOME, width=1.75),
    *(_key("home", letter, letter, 1.75 + index, Y_HOME) for index, letter in enumerate("ASDFGHJKL")),
    _key("home", "SEMICOLON", ";", 10.75, Y_HOME, secondary=":"),
    _key("home", "APOSTROPHE", "'", 11.75, Y_HOME, secondary='"'),
    _key("home", "ENTER", "ENTER", 12.75, Y_HOME, width=2.25, secondary="<-"),
    _key("home", "KP4", "4", 15.30, Y_HOME, secondary="<"),
    _key("home", "KP5", "5", 16.30, Y_HOME),
    _key("home", "KP6", "6", 17.30, Y_HOME, secondary=">"),
)

SHIFT_ROW = (
    _key("shift", "LSHIFT", "SHIFT", 0.00, Y_SHIFT, width=2.25, secondary="^"),
    *(_key("shift", letter, letter, 2.25 + index, Y_SHIFT) for index, letter in enumerate("ZXCVBNM")),
    _key("shift", "COMMA", ",", 9.25, Y_SHIFT, secondary="<"),
    _key("shift", "PERIOD", ".", 10.25, Y_SHIFT, secondary=">"),
    _key("shift", "SLASH", "/", 11.25, Y_SHIFT, secondary="?"),
    _key("shift", "RSHIFT", "SHIFT", 12.25, Y_SHIFT, width=2.75, secondary="^"),
    _key("shift", "KP1", "1", 15.30, Y_SHIFT, secondary="END"),
    _key("shift", "KP2", "2", 16.30, Y_SHIFT, secondary="v"),
    _key("shift", "KP3", "3", 17.30, Y_SHIFT, secondary="PGDN"),
    _key("shift", "KP_ENTER", "ENTER", 18.30, Y_SHIFT, height=2.10),
)

BOTTOM_ROW = (
    _key("bottom", "LCTRL", "CTRL", 0.00, Y_BOTTOM, width=1.25),
    _key("bottom", "FN", "FN", 1.25, Y_BOTTOM),
    _key("bottom", "LWIN", "WIN", 2.25, Y_BOTTOM),
    _key("bottom", "LALT", "ALT", 3.25, Y_BOTTOM, width=1.25),
    _key("bottom", "SPACE", "", 4.50, Y_BOTTOM, width=6.25),
    _key("bottom", "RALT", "ALT", 10.75, Y_BOTTOM, width=1.25),
    _key("bottom", "RCTRL", "CTRL", 12.00, Y_BOTTOM, width=0.95),
    _key("bottom", "LEFT", "<", 12.95, Y_BOTTOM + 0.52, width=0.70, height=0.48),
    _key("bottom", "UP", "^", 13.65, Y_BOTTOM, width=0.70, height=0.48),
    _key("bottom", "DOWN", "v", 13.65, Y_BOTTOM + 0.52, width=0.70, height=0.48),
    _key("bottom", "RIGHT", ">", 14.35, Y_BOTTOM + 0.52, width=0.70, height=0.48),
    _key("bottom", "KP0", "0", 15.30, Y_BOTTOM, width=2.00, secondary="INS"),
    _key("bottom", "KP_DECIMAL", ".", 17.30, Y_BOTTOM, secondary="DEL"),
)

KEYBOARD_ROWS = {
    "function": FUNCTION_ROW,
    "number": NUMBER_ROW,
    "qwerty": QWERTY_ROW,
    "home": HOME_ROW,
    "shift": SHIFT_ROW,
    "bottom": BOTTOM_ROW,
}
KEYBOARD_KEYS = tuple(key for row in KEYBOARD_ROWS.values() for key in row)
KEYBOARD_KEYS_BY_ID = {key.id: key for key in KEYBOARD_KEYS}

_WASD_IDS = frozenset({"W", "A", "S", "D"})
_CENTER_BOUNDARY_IDS = frozenset(
    {"F12", "EQUAL", "RBRACKET", "APOSTROPHE", "SLASH", "RCTRL"}
)


def key_zone(key: KeyboardKey) -> Zone:
    """Return an approximate fixed display zone, never a control capability."""
    if key.id in _WASD_IDS:
        return Zone.WASD
    if key.id in _CENTER_BOUNDARY_IDS:
        return Zone.CENTER
    if key.x < 5.25:
        return Zone.LEFT
    if key.x < 11.25:
        return Zone.CENTER
    return Zone.RIGHT


def key_zone_by_id(key_id: str) -> Zone:
    """Resolve a physical key identifier through the authoritative zone model."""
    return key_zone(KEYBOARD_KEYS_BY_ID[key_id])


def block_key_legend(key: KeyboardKey, interior_width: int) -> str:
    """Return an unambiguous legend that fits a filled block keycap."""
    aliases = {
        "ESC": "Esc",
        "OMEN": "◆",
        "CALCULATOR": "CALC",
        "PRINT": "PRT",
        "NUMLOCK": "NUM",
        "BACKSPACE": "Bksp",
        "CAPSLOCK": "Caps",
        "LSHIFT": "Shift",
        "RSHIFT": "Shift",
        "LCTRL": "Ctrl",
        "RCTRL": "Ctrl",
        "POWER": "Pwr",
        "DELETE": "Del",
        "INSERT": "Ins",
        "KP_ENTER": "Ent",
    }
    legend = aliases.get(key.id, key.legend)
    if len(legend) <= interior_width:
        return legend
    if key.id.startswith("F") and key.id[1:].isdigit():
        number = key.id[1:]
        legend = number if interior_width <= 2 else key.id
    return legend[: max(0, interior_width)]


def project_block_keyboard(width: int, height: int) -> BlockKeyboardProjection:
    """Project the photo geometry into filled rectangular keycaps.

    One-cell gutters are removed from the right and bottom of each normalized
    footprint.  Key coordinates always come from the physical model; the
    renderer never repacks rows to make them fit.
    """
    if width < 39 or height < 7:
        raise ValueError("keyboard block projection needs at least 39 x 7 cells")

    model_right = max(key.x + key.width for key in KEYBOARD_KEYS)
    model_bottom = max(key.y + key.height for key in KEYBOARD_KEYS)
    unit_x = max(
        2,
        min(
            5,
            max(
                unit
                for unit in range(2, 6)
                if int(model_right * unit + 0.5) <= width
            ),
        ),
    )
    unit_y = 3 if int(model_bottom * 3 + 0.5) <= height else 2
    if int(model_bottom * unit_y + 0.5) > height:
        unit_y = 1

    raw_width = int(model_right * unit_x + 0.5)
    origin_x = max(0, (width - raw_width) // 2)

    def scale_x(value: float) -> int:
        return origin_x + int(value * unit_x + 0.5)

    def scale_y(value: float) -> int:
        return int(value * unit_y + 0.5)

    projected = []
    for key in KEYBOARD_KEYS:
        left = scale_x(key.x)
        right = scale_x(key.x + key.width)
        top = scale_y(key.y)
        bottom = scale_y(key.y + key.height)
        projected.append(
            BlockProjectedKey(
                key=key,
                x=left,
                y=top,
                width=max(1, right - left - 1),
                height=max(1, bottom - top - 1),
                zone=key_zone(key),
            )
        )

    return BlockKeyboardProjection(
        width=width,
        height=height,
        unit_x=unit_x,
        unit_y=unit_y,
        used_width=max(item.right for item in projected),
        keys=tuple(projected),
    )


def render_block_keyboard(
    width: int,
    height: int,
    selected_zone: Zone,
) -> BlockKeyboardBuffer:
    """Render filled keycaps while retaining cell-level key and zone identity."""
    projection = project_block_keyboard(width, height)
    characters = [[" " for _ in range(width)] for _ in range(height)]
    key_ids: list[list[str | None]] = [[None for _ in range(width)] for _ in range(height)]
    zones: list[list[Zone | None]] = [[None for _ in range(width)] for _ in range(height)]
    selected = [[False for _ in range(width)] for _ in range(height)]

    for item in projection.keys:
        is_selected = item.zone is selected_zone
        for row in range(item.y, item.bottom):
            for column in range(item.x, item.right):
                characters[row][column] = "▀" if item.height > 1 and row == item.y else " "
                key_ids[row][column] = item.key.id
                zones[row][column] = item.zone
                selected[row][column] = is_selected

        legend = block_key_legend(item.key, item.width)
        legend_row = item.bottom - 1
        legend_column = item.x + max(0, (item.width - len(legend)) // 2)
        for offset, character in enumerate(legend[: item.width]):
            column = legend_column + offset
            characters[legend_row][column] = character

    return BlockKeyboardBuffer(
        width=width,
        height=height,
        lines=tuple("".join(row) for row in characters),
        key_ids=tuple(tuple(row) for row in key_ids),
        zones=tuple(tuple(row) for row in zones),
        selected=tuple(tuple(row) for row in selected),
    )


def project_keyboard(terminal_width: int) -> KeyboardProjection:
    """Project normalized photo geometry into a responsive perspective grid."""
    available = max(0, terminal_width - 4)
    if available >= 105:
        unit_width = 5
    elif available >= 84:
        unit_width = 4
    else:
        unit_width = 3

    row_origins = {
        "function": 5,
        "number": 4,
        "qwerty": 3,
        "home": 2,
        "shift": 1,
        "bottom": 0,
    }
    screen_rows = {
        "function": 0,
        "number": 1,
        "qwerty": 2,
        "home": 3,
        "shift": 4,
        "bottom": 6,
    }

    def scale(value: float) -> int:
        return int(value * unit_width + 0.5)

    projected = []
    max_right = 0
    for key in KEYBOARD_KEYS:
        screen_row = screen_rows[key.row]
        if key.id == "UP":
            screen_row = 5
        left = scale(key.x)
        right = scale(key.x + key.width)
        screen_x = row_origins[key.row] + left
        width = max(1, right - left)
        row_span = 2 if key.height > 1.0 else 1
        item = ProjectedKey(
            key=key,
            screen_x=screen_x,
            screen_row=screen_row,
            width=width,
            row_span=row_span,
            zone=key_zone(key),
        )
        projected.append(item)
        max_right = max(max_right, screen_x + width)

    return KeyboardProjection(
        unit_width=unit_width,
        width=ceil(max_right + 1),
        row_origins=row_origins,
        keys=tuple(projected),
    )
