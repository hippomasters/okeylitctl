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
    workspace_frame,
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
        self.assertEqual(handle_key(self.state, ord("m")), TuiCommand.PROFILES)
        self.assertEqual(handle_key(self.state, ord("q")), TuiCommand.QUIT)

    def test_undersized_mode_suppresses_every_action_except_quit(self):
        self.state.set_selected_color("FFFFFF")

        for key in (ord("a"), ord("o"), ord("r"), ord("e"), ord("m"), ord("p")):
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

    def test_workspace_is_centered_and_width_capped_on_large_terminals(self):
        minimum = workspace_frame(24, 78)
        self.assertEqual((minimum.x, minimum.y, minimum.width), (0, 0, 78))

        large = workspace_frame(60, 180)
        self.assertEqual(large.width, 132)
        self.assertEqual(large.x, 24)
        self.assertEqual(large.y, 18)

        medium = workspace_frame(30, 100)
        self.assertEqual(medium.y, 3)

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


class FakeProfileStore:
    def __init__(self):
        self.calls = []
        self.layouts = {
            "Work": ColorLayout.from_wire("ABCDEF,123456,654321,FEDCBA")
        }

    def list_names(self):
        self.calls.append(("list",))
        return tuple(sorted(self.layouts))

    def load(self, name):
        self.calls.append(("load", name))
        return self.layouts[name]

    def save(self, name, layout, *, overwrite=False):
        self.calls.append(("save", name, layout.to_wire(), overwrite))
        self.layouts[name] = layout

    def delete(self, name):
        self.calls.append(("delete", name))
        del self.layouts[name]


class TuiDeviceActionTests(unittest.TestCase):
    def test_confirmation_accepts_only_explicit_yes(self):
        app = CursesTui(FakeBackend(), profile_store=FakeProfileStore())

        class ConfirmScreen:
            def __init__(self, key):
                self.key = key

            def getmaxyx(self):
                return (30, 100)

            def addnstr(self, *_args):
                return None

            def refresh(self):
                return None

            def getch(self):
                return self.key

        self.assertTrue(app._confirm_action(ConfirmScreen(ord("y")), "Apply layout?"))
        for key in (ord("n"), 27, ord("q"), curses.KEY_RESIZE):
            with self.subTest(key=key):
                self.assertFalse(
                    app._confirm_action(ConfirmScreen(key), "Apply layout?")
                )

    def test_confirmation_fails_closed_when_prompt_cannot_render(self):
        app = CursesTui(FakeBackend(), profile_store=FakeProfileStore())

        class HiddenPromptScreen:
            def getmaxyx(self):
                return (30, 100)

            def addnstr(self, *_args):
                raise curses.error

            def refresh(self):
                return None

            def getch(self):
                return ord("y")

        class MissingGeometryScreen:
            def getmaxyx(self):
                raise curses.error

        self.assertFalse(
            app._confirm_action(HiddenPromptScreen(), "Apply complete layout?")
        )
        self.assertFalse(
            app._confirm_action(MissingGeometryScreen(), "Apply complete layout?")
        )

        class TruncatedPromptScreen:
            def __init__(self):
                self.reads = 0

            def getmaxyx(self):
                return (7, 20)

            def addnstr(self, *_args):
                return None

            def refresh(self):
                return None

            def getch(self):
                self.reads += 1
                return ord("y")

        truncated = TruncatedPromptScreen()
        self.assertFalse(
            app._confirm_action(truncated, "Apply complete four-zone layout?")
        )
        self.assertEqual(truncated.reads, 0)

    def test_apply_is_immediate_and_quits_cleanly(self):
        backend = FakeBackend()
        app = CursesTui(backend, profile_store=FakeProfileStore())
        app.state.set_selected_color("A1B2C3")

        class ScriptedScreen:
            def __init__(self):
                self.keys = iter((ord("a"), ord("q")))

            def keypad(self, _enabled):
                return None

            def getmaxyx(self):
                return (30, 110)

            def erase(self):
                return None

            def addnstr(self, *_args):
                return None

            def refresh(self):
                return None

            def getch(self):
                return next(self.keys)

        with mock.patch("okeylitctl.tui.initialize_colors", return_value=False):
            app._main(ScriptedScreen())

        self.assertEqual(backend.writes, ["A1B2C3,222222,333333,444444"])
        self.assertFalse(app.state.dirty)

    def test_apply_does_not_consume_a_followup_key_as_confirmation(self):
        backend = FakeBackend()
        app = CursesTui(backend, profile_store=FakeProfileStore())
        app.state.set_selected_color("A1B2C3")

        class ScriptedScreen:
            def __init__(self):
                self.keys = iter((ord("a"), ord("n"), ord("q")))

            def keypad(self, _enabled):
                return None

            def getmaxyx(self):
                return (30, 110)

            def erase(self):
                return None

            def addnstr(self, *_args):
                return None

            def refresh(self):
                return None

            def getch(self):
                return next(self.keys)

        with mock.patch("okeylitctl.tui.initialize_colors", return_value=False):
            app._main(ScriptedScreen())

        self.assertEqual(backend.writes, ["A1B2C3,222222,333333,444444"])
        self.assertFalse(app.state.dirty)

    def test_cancelled_restore_and_dirty_quit_never_change_firmware(self):
        backend = FakeBackend()
        app = CursesTui(backend, profile_store=FakeProfileStore())
        app.state.set_selected_color("A1B2C3")

        class ScriptedScreen:
            def __init__(self):
                self.keys = iter(
                    (
                        ord("o"),
                        ord("n"),
                        ord("q"),
                        ord("n"),
                        ord("q"),
                        ord("y"),
                    )
                )

            def keypad(self, _enabled):
                return None

            def getmaxyx(self):
                return (30, 110)

            def erase(self):
                return None

            def addnstr(self, *_args):
                return None

            def refresh(self):
                return None

            def getch(self):
                return next(self.keys)

        with mock.patch("okeylitctl.tui.initialize_colors", return_value=False):
            app._main(ScriptedScreen())

        self.assertEqual(backend.restores, 0)
        self.assertEqual(backend.writes, [])
        self.assertTrue(app.state.dirty)

    def test_dirty_quit_remains_usable_after_terminal_shrinks(self):
        backend = FakeBackend()
        app = CursesTui(backend, profile_store=FakeProfileStore())
        app.state.set_selected_color("A1B2C3")

        class NarrowScreen:
            def __init__(self, width, keys):
                self.width = width
                self.keys = iter(keys)
                self.reads = 0
                self.rendered = []

            def keypad(self, _enabled):
                return None

            def getmaxyx(self):
                return (7, self.width)

            def erase(self):
                return None

            def addnstr(self, _y, _x, text, limit, _attr):
                self.rendered.append(text[:limit])

            def refresh(self):
                return None

            def getch(self):
                self.reads += 1
                return next(self.keys)

        confirmable = NarrowScreen(20, (ord("q"), ord("n"), ord("q"), ord("y")))
        with mock.patch("okeylitctl.tui.initialize_colors", return_value=False):
            app._main(confirmable)
        self.assertIn("Quit?", confirmable.rendered)
        self.assertIn("Y=yes; else=no", confirmable.rendered)
        self.assertEqual(confirmable.reads, 4)

        too_small = NarrowScreen(19, (ord("q"),))
        with mock.patch("okeylitctl.tui.initialize_colors", return_value=False):
            app._main(too_small)
        self.assertEqual(too_small.reads, 1)

    def test_main_and_draw_exit_safely_on_terminal_io_errors(self):
        app = CursesTui(FakeBackend(), profile_store=FakeProfileStore())

        class FailingInputScreen:
            def keypad(self, _enabled):
                return None

            def getmaxyx(self):
                return (30, 110)

            def erase(self):
                return None

            def addnstr(self, *_args):
                return None

            def refresh(self):
                return None

            def getch(self):
                raise curses.error

        class FailingDrawScreen:
            def getmaxyx(self):
                return (30, 110)

            def erase(self):
                raise curses.error

        class FailingGeometryScreen:
            def keypad(self, _enabled):
                return None

            def getmaxyx(self):
                raise curses.error

            def erase(self):
                return None

            def getch(self):
                return ord("q")

        with mock.patch("okeylitctl.tui.initialize_colors", return_value=False):
            app._main(FailingInputScreen())
        app._draw(FailingDrawScreen())
        app._draw(FailingGeometryScreen())
        with mock.patch.object(app, "_draw"):
            app._main(FailingGeometryScreen())

    def test_profile_load_changes_only_the_local_draft(self):
        backend = FakeBackend()
        profiles = FakeProfileStore()
        app = CursesTui(backend, profile_store=profiles)

        app._load_profile("Work")

        self.assertEqual(
            app.state.draft.to_wire(), "ABCDEF,123456,654321,FEDCBA"
        )
        self.assertEqual(
            app.state.current.to_wire(), "111111,222222,333333,444444"
        )
        self.assertEqual(backend.writes, [])
        self.assertEqual(profiles.calls, [("load", "Work")])
        self.assertIn("locally", app.state.message)

    def test_profile_save_persists_the_local_draft_without_applying(self):
        backend = FakeBackend()
        profiles = FakeProfileStore()
        app = CursesTui(backend, profile_store=profiles)
        app.state.set_selected_color("A1B2C3")

        app._save_profile("Draft")

        self.assertEqual(backend.writes, [])
        self.assertEqual(
            profiles.calls,
            [("save", "Draft", "A1B2C3,222222,333333,444444", False)],
        )
        self.assertIn("Draft", app.state.message)

    def test_profile_delete_does_not_touch_the_device(self):
        backend = FakeBackend()
        profiles = FakeProfileStore()
        app = CursesTui(backend, profile_store=profiles)

        app._delete_profile("Work")

        self.assertEqual(backend.writes, [])
        self.assertEqual(profiles.calls, [("delete", "Work")])
        self.assertIn("Work", app.state.message)

    def test_profile_manager_enter_loads_selected_profile_locally(self):
        backend = FakeBackend()
        profiles = FakeProfileStore()
        app = CursesTui(backend, profile_store=profiles)

        class MenuScreen:
            def getmaxyx(self):
                return (30, 100)

            def addnstr(self, *_args):
                return None

            def refresh(self):
                return None

            def getch(self):
                return 10

        app._profile_manager(MenuScreen())

        self.assertEqual(
            app.state.draft.to_wire(), "ABCDEF,123456,654321,FEDCBA"
        )
        self.assertEqual(profiles.calls, [("list",), ("load", "Work")])
        self.assertEqual(backend.writes, [])

    def test_profile_manager_saves_named_draft_without_applying(self):
        backend = FakeBackend()
        profiles = FakeProfileStore()
        app = CursesTui(backend, profile_store=profiles)
        app.state.set_selected_color("A1B2C3")

        class MenuScreen:
            def __init__(self):
                self.keys = iter((ord("s"), 27))

            def getmaxyx(self):
                return (30, 100)

            def addnstr(self, *_args):
                return None

            def refresh(self):
                return None

            def getch(self):
                return next(self.keys)

            def getstr(self, *_args):
                return b"Draft"

        with mock.patch("okeylitctl.tui.curses.echo"), mock.patch(
            "okeylitctl.tui.curses.noecho"
        ):
            app._profile_manager(MenuScreen())

        self.assertEqual(backend.writes, [])
        self.assertIn(
            ("save", "Draft", "A1B2C3,222222,333333,444444", False),
            profiles.calls,
        )

    def test_profile_manager_requires_confirmation_before_delete(self):
        backend = FakeBackend()
        profiles = FakeProfileStore()
        app = CursesTui(backend, profile_store=profiles)

        class MenuScreen:
            def __init__(self):
                self.keys = iter((ord("d"), ord("y"), 27))

            def getmaxyx(self):
                return (30, 100)

            def addnstr(self, *_args):
                return None

            def refresh(self):
                return None

            def getch(self):
                return next(self.keys)

        app._profile_manager(MenuScreen())

        self.assertEqual(backend.writes, [])
        self.assertIn(("delete", "Work"), profiles.calls)

    def test_profile_manager_render_failure_cannot_trigger_hidden_delete(self):
        backend = FakeBackend()
        profiles = FakeProfileStore()
        app = CursesTui(backend, profile_store=profiles)

        class HiddenMenuScreen:
            def getmaxyx(self):
                return (30, 100)

            def addnstr(self, *_args):
                raise curses.error("cannot render menu")

            def refresh(self):
                return None

            def getch(self):
                return ord("y")

        app._profile_manager(HiddenMenuScreen())

        self.assertNotIn(("delete", "Work"), profiles.calls)
        self.assertEqual(backend.writes, [])
        self.assertIn("rendering error", app.state.message)

    def test_profile_manager_resize_cannot_trigger_truncated_delete(self):
        backend = FakeBackend()
        profiles = FakeProfileStore()
        app = CursesTui(backend, profile_store=profiles)

        class ResizedMenuScreen:
            def __init__(self):
                self.width = 100
                self.keys = iter((ord("d"), ord("y")))
                self.reads = 0

            def getmaxyx(self):
                return (30, self.width)

            def addnstr(self, *_args):
                return None

            def refresh(self):
                return None

            def getch(self):
                self.reads += 1
                key = next(self.keys)
                self.width = 20
                return key

        screen = ResizedMenuScreen()
        app._profile_manager(screen)

        self.assertNotIn(("delete", "Work"), profiles.calls)
        self.assertEqual(screen.reads, 1)
        self.assertIn("rendering error", app.state.message)

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

    def test_large_workspace_is_centered_with_live_draft_editor_and_command_bars(self):
        backend = FakeBackend()
        app = CursesTui(backend)
        app.state.set_selected_color("A1B2C3")

        class BufferScreen:
            def __init__(self):
                self.width = 180
                self.height = 60
                self.cells = [
                    [" " for _ in range(self.width)] for _ in range(self.height)
                ]

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

        screen = BufferScreen()
        app._draw(screen)
        frame = workspace_frame(screen.height, screen.width)
        visible = "\n".join(screen.row(y) for y in range(frame.y, frame.y + 24))
        zone_area = "\n".join(
            screen.row(y) for y in range(frame.y + 5, frame.y + 11)
        )

        self.assertIn("OKEYLITCTL", screen.row(frame.y))
        self.assertNotIn("OKEYLITCTL", screen.row(1))
        self.assertIn("▶ 1 · RIGHT + ARROWS + NUMPAD", zone_area)
        self.assertIn("LIVE #111111", zone_area)
        self.assertIn("DRAFT #A1B2C3", zone_area)
        self.assertIn("COLOR EDITOR", visible)
        self.assertIn("DRAFT #A1B2C3", visible)
        self.assertIn("LIVE  #111111", visible)
        self.assertIn("[A] APPLY", visible)
        self.assertIn("[M] PROFILES", visible)

    def test_minimum_workspace_preserves_borders_and_complete_safety_labels(self):
        backend = FakeBackend()
        app = CursesTui(backend)
        app.state.draft = app.state.draft.with_zone(Zone.LEFT, "ABCDEF")

        class BufferScreen:
            def __init__(self):
                self.width = 78
                self.height = 24
                self.cells = [
                    [" " for _ in range(self.width)] for _ in range(self.height)
                ]

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

        screen = BufferScreen()
        app._draw(screen)
        zone_area = "\n".join(screen.row(y) for y in range(5, 11))

        self.assertIn("NOT PER-KEY", screen.row(2))
        self.assertIn("3 LEFT *", zone_area)
        self.assertIn("D #ABCDEF *", zone_area)
        self.assertIn("1 RIGHT+ARROWS+NUMPAD", zone_area)

        app.state.selected_zone = Zone.LEFT
        app._draw(screen)
        editor = "\n".join(screen.row(y) for y in range(12, 21))
        self.assertIn("A applies immediately", editor)
        self.assertIn("[P] preset  [E] hex  [X] discard", editor)
        self.assertEqual(screen.cells[12][2], "╭")
        self.assertEqual(screen.cells[12][75], "╮")
        for row in range(13, 20):
            self.assertEqual(screen.cells[row][2], "│")
            self.assertEqual(screen.cells[row][75], "│")
        self.assertEqual(screen.cells[20][2], "╰")
        self.assertEqual(screen.cells[20][75], "╯")

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
                top = workspace_frame(screen.height, width).y
                regions = keyboard_region_geometry(width, top=top)

                for zone in (Zone.LEFT, Zone.CENTER, Zone.RIGHT):
                    region = regions[zone]
                    for y in range(region.y + 1, region.y + region.height - 1):
                        self.assertEqual(screen.cells[y][region.x], "│")
                        self.assertEqual(
                            screen.cells[y][region.x + region.width - 1], "│"
                        )

                visualization = "\n".join(
                    screen.row(y) for y in range(top + 5, top + 11)
                )
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

    def test_truncated_text_entry_prompts_do_not_accept_hidden_input(self):
        backend = FakeBackend()
        app = CursesTui(backend, profile_store=FakeProfileStore())
        original = app.state.draft

        class NarrowInputScreen:
            def __init__(self):
                self.reads = 0

            def getmaxyx(self):
                return (7, 20)

            def addnstr(self, *_args):
                return None

            def refresh(self):
                return None

            def getstr(self, *_args):
                self.reads += 1
                return b"ABCDEF"

        profile_screen = NarrowInputScreen()
        color_screen = NarrowInputScreen()
        with mock.patch("okeylitctl.tui.curses.echo"), mock.patch(
            "okeylitctl.tui.curses.noecho"
        ), mock.patch("okeylitctl.tui.curses.curs_set"):
            self.assertIsNone(app._prompt_profile_name(profile_screen))
            app._edit_color(color_screen)

        self.assertEqual(profile_screen.reads, 0)
        self.assertEqual(color_screen.reads, 0)
        self.assertEqual(app.state.draft, original)


if __name__ == "__main__":
    unittest.main()
