import curses
import unittest
from unittest import mock

from okeylitctl.models import ColorLayout, Zone
from okeylitctl.tui import (
    PRESETS,
    CursesTui,
    TuiCommand,
    handle_key,
    initialize_colors,
    keyboard_region_geometry,
    rgb_to_xterm_index,
    set_cursor_visibility,
)
from okeylitctl.tui_state import TuiState


class TuiInteractionTests(unittest.TestCase):
    def setUp(self):
        self.state = TuiState(
            current=ColorLayout.from_wire("111111,222222,333333,444444"),
            original=ColorLayout.from_wire("AAAAAA,BBBBBB,CCCCCC,DDDDDD"),
        )

    def test_navigation_and_channel_keys_only_change_local_state(self):
        self.assertEqual(handle_key(self.state, curses.KEY_RIGHT), TuiCommand.NONE)
        self.assertEqual(self.state.selected_zone, Zone.CENTER)
        self.assertEqual(handle_key(self.state, curses.KEY_DOWN), TuiCommand.NONE)
        self.assertEqual(self.state.selected_channel, 1)
        self.assertEqual(handle_key(self.state, ord("+")), TuiCommand.NONE)
        self.assertEqual(self.state.draft.center, "222322")
        self.assertEqual(self.state.current.center, "222222")

    def test_action_keys_return_commands_without_touching_firmware(self):
        self.assertEqual(handle_key(self.state, ord("a")), TuiCommand.APPLY)
        self.assertEqual(handle_key(self.state, ord("o")), TuiCommand.RESTORE)
        self.assertEqual(handle_key(self.state, ord("r")), TuiCommand.REFRESH)
        self.assertEqual(handle_key(self.state, ord("e")), TuiCommand.EDIT)
        self.assertEqual(handle_key(self.state, ord("q")), TuiCommand.QUIT)

    def test_undersized_mode_suppresses_every_action_except_quit(self):
        self.state.set_selected_color("FFFFFF")

        for key in (ord("a"), ord("o"), ord("r"), ord("e"), ord("p")):
            with self.subTest(key=key):
                self.assertEqual(
                    handle_key(self.state, key, actions_enabled=False),
                    TuiCommand.NONE,
                )
        self.assertEqual(
            handle_key(self.state, ord("q"), actions_enabled=False),
            TuiCommand.QUIT,
        )

    def test_preset_key_cycles_a_complete_local_layout(self):
        self.assertEqual(handle_key(self.state, ord("p")), TuiCommand.NONE)
        self.assertEqual(self.state.draft, PRESETS[0][1])
        self.assertTrue(self.state.dirty)
        self.assertIn(PRESETS[0][0], self.state.message)

        handle_key(self.state, ord("p"))
        self.assertEqual(self.state.draft, PRESETS[1][1])

    def test_discard_and_help_are_local_actions(self):
        self.state.set_selected_color("FFFFFF")
        handle_key(self.state, ord("x"))
        self.assertFalse(self.state.dirty)

        self.assertFalse(self.state.help_visible)
        handle_key(self.state, ord("?"))
        self.assertTrue(self.state.help_visible)
        handle_key(self.state, 27)
        self.assertFalse(self.state.help_visible)

    def test_rgb_to_xterm_cube_mapping(self):
        self.assertEqual(rgb_to_xterm_index("000000"), 16)
        self.assertEqual(rgb_to_xterm_index("FF0000"), 196)
        self.assertEqual(rgb_to_xterm_index("FFFFFF"), 231)

    def test_keyboard_visualization_uses_regions_not_individual_keys(self):
        regions = keyboard_region_geometry(100)
        left = regions[Zone.LEFT]
        center = regions[Zone.CENTER]
        right = regions[Zone.RIGHT]
        wasd = regions[Zone.WASD]

        self.assertLess(left.x + left.width, center.x)
        self.assertLess(center.x + center.width, right.x)
        self.assertGreater(right.width, left.width)
        self.assertGreaterEqual(wasd.x, left.x)
        self.assertLessEqual(wasd.x + wasd.width, left.x + left.width)
        self.assertGreaterEqual(wasd.y, left.y)
        self.assertLessEqual(wasd.y + wasd.height, left.y + left.height)

    def test_cursor_visibility_is_an_optional_terminal_capability(self):
        with mock.patch("okeylitctl.tui.curses.curs_set", side_effect=curses.error):
            set_cursor_visibility(0)

    def test_color_initialization_fails_closed_on_terminal_capability_errors(self):
        with mock.patch(
            "okeylitctl.tui.curses.has_colors", side_effect=curses.error
        ):
            self.assertFalse(initialize_colors())

        with mock.patch("okeylitctl.tui.curses.has_colors", return_value=True), mock.patch(
            "okeylitctl.tui.curses.start_color", side_effect=curses.error
        ):
            self.assertFalse(initialize_colors())

        with mock.patch("okeylitctl.tui.curses.has_colors", return_value=True), mock.patch(
            "okeylitctl.tui.curses.start_color"
        ), mock.patch(
            "okeylitctl.tui.curses.use_default_colors", side_effect=curses.error
        ), mock.patch("okeylitctl.tui.curses.init_pair", side_effect=curses.error):
            self.assertFalse(initialize_colors())


class FakeBackend:
    def __init__(self, *, same_as_original=False):
        colors = ["111111", "222222", "333333", "444444"]
        original = colors if same_as_original else ["AAAAAA", "BBBBBB", "CCCCCC", "DDDDDD"]
        self.status_value = {"state": "on", "colors": colors, "original": original}
        self.writes = []
        self.restores = 0

    def status(self):
        return self.status_value

    def write_colors(self, value):
        self.writes.append(value)

    def restore(self):
        self.restores += 1


class TuiDeviceActionTests(unittest.TestCase):
    def test_workspace_draws_coarse_keyboard_regions(self):
        backend = FakeBackend()
        app = CursesTui(backend)

        class RecordingScreen:
            def __init__(self):
                self.text = []

            def getmaxyx(self):
                return (30, 110)

            def erase(self):
                return None

            def addnstr(self, _y, _x, text, _limit, _attr):
                self.text.append(text)

            def refresh(self):
                return None

        screen = RecordingScreen()
        app._draw(screen)
        rendered = " ".join(screen.text)

        self.assertIn("LEFT REGION", rendered)
        self.assertIn("WASD REGION", rendered)
        self.assertIn("CENTER REGION", rendered)
        self.assertIn("RIGHT + ARROWS + NUMPAD", rendered)
        self.assertNotIn("BACKSPACE", rendered)

    def test_keyboard_region_rendering_preserves_boundaries_and_all_four_colors(self):
        backend = FakeBackend()
        app = CursesTui(backend)

        class BufferScreen:
            def __init__(self, width):
                self.width = width
                self.height = 30
                self.cells = [[" " for _ in range(width)] for _ in range(self.height)]

            def getmaxyx(self):
                return (self.height, self.width)

            def erase(self):
                self.cells = [
                    [" " for _ in range(self.width)] for _ in range(self.height)
                ]

            def addnstr(self, y, x, text, limit, _attr):
                for offset, character in enumerate(text[:limit]):
                    if 0 <= y < self.height and 0 <= x + offset < self.width:
                        self.cells[y][x + offset] = character

            def refresh(self):
                return None

            def row(self, y):
                return "".join(self.cells[y])

        for width in (78, 79, 100, 140):
            with self.subTest(width=width):
                screen = BufferScreen(width)
                app._draw(screen)
                regions = keyboard_region_geometry(width)

                for zone in (Zone.LEFT, Zone.CENTER, Zone.RIGHT):
                    region = regions[zone]
                    for y in range(region.y + 1, region.y + region.height - 1):
                        self.assertEqual(screen.cells[y][region.x], "│")
                        self.assertEqual(
                            screen.cells[y][region.x + region.width - 1], "│"
                        )

                visualization = "\n".join(screen.row(y) for y in range(5, 11))
                self.assertIn("#333333", visualization)
                self.assertIn("#444444", visualization)
                self.assertIn("WASD REGION", visualization)

    def test_apply_refuses_to_write_when_draft_is_unchanged(self):
        backend = FakeBackend()
        app = CursesTui(backend)

        app._apply()

        self.assertEqual(backend.writes, [])
        self.assertIn("No unapplied", app.state.message)

    def test_restore_refuses_to_write_when_original_is_already_active(self):
        backend = FakeBackend(same_as_original=True)
        app = CursesTui(backend)

        app._restore()

        self.assertEqual(backend.restores, 0)
        self.assertIn("already active", app.state.message)

    def test_apply_writes_one_complete_layout_then_marks_it_current(self):
        backend = FakeBackend()
        app = CursesTui(backend)
        app.state.set_selected_color("ABCDEF")

        app._apply()

        self.assertEqual(backend.writes, ["ABCDEF,222222,333333,444444"])
        self.assertEqual(app.state.current, app.state.draft)

    def test_exact_color_input_failure_returns_to_workspace(self):
        backend = FakeBackend()
        app = CursesTui(backend)

        class FailingScreen:
            def getmaxyx(self):
                return (30, 100)

            def addnstr(self, *args):
                return None

            def refresh(self):
                return None

            def getstr(self, *args):
                raise curses.error("resized")

        with mock.patch("okeylitctl.tui.curses.echo"), mock.patch(
            "okeylitctl.tui.curses.noecho"
        ), mock.patch("okeylitctl.tui.curses.curs_set", side_effect=curses.error):
            app._edit_color(FailingScreen())

        self.assertIn("cancelled", app.state.message.lower())

    def test_exact_color_prompt_refresh_failure_returns_to_workspace(self):
        backend = FakeBackend()
        app = CursesTui(backend)

        class RefreshFailingScreen:
            def getmaxyx(self):
                return (30, 100)

            def addnstr(self, *args):
                return None

            def refresh(self):
                raise curses.error("resized")

        app._edit_color(RefreshFailingScreen())

        self.assertIn("cancelled", app.state.message.lower())


if __name__ == "__main__":
    unittest.main()
