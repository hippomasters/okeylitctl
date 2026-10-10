import curses
import unittest
from unittest import mock

from okeylitctl import tui as tui_module
from okeylitctl.models import ColorLayout, Zone
from okeylitctl.keyboard_layout import key_zone_by_id
from okeylitctl.profiles import ProfileError
from okeylitctl.sysfs import BackendIOError
from okeylitctl.effects import EffectKind
from okeylitctl.ipc import ConflictError
from okeylitctl.tui_compositor import (
    compose_editor, compose_adaptive_editor, compose_narrow_editor,
    compose_profiles, compose_responsive_profiles, compose_exact_hex_modal,
)
from okeylitctl.tui import (
    _CHARACTER_KEY_IDS,
    _SPECIAL_KEY_IDS,
    PRESETS,
    CursesTui,
    TuiCommand,
    effect_zone_for_key,
    handle_key,
    initialize_colors,
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

    def test_effect_and_speed_controls_are_local_and_follow_mockup_order(self):
        self.assertEqual(self.state.effect_kind, EffectKind.STATIC)
        self.assertEqual(handle_key(self.state, 9), TuiCommand.NONE)
        self.assertEqual(self.state.effect_kind, EffectKind.BLINK)
        self.assertEqual(handle_key(self.state, curses.KEY_BTAB), TuiCommand.NONE)
        self.assertEqual(self.state.effect_kind, EffectKind.STATIC)

        initial_speed = self.state.effect_speed
        self.assertEqual(handle_key(self.state, ord("]")), TuiCommand.NONE)
        self.assertGreater(self.state.effect_speed, initial_speed)
        self.assertEqual(handle_key(self.state, ord("[")), TuiCommand.NONE)
        self.assertEqual(self.state.effect_speed, initial_speed)

        initial_light = self.state.effect_light
        self.assertEqual(handle_key(self.state, ord("}")), TuiCommand.NONE)
        self.assertGreater(self.state.effect_light, initial_light)
        self.assertEqual(handle_key(self.state, ord("{")), TuiCommand.NONE)
        self.assertEqual(self.state.effect_light, initial_light)
        self.assertEqual(self.state.effect_direction, 1)
        self.assertEqual(handle_key(self.state, ord("d")), TuiCommand.NONE)
        self.assertEqual(self.state.effect_direction, -1)
        self.assertEqual(self.state.current, ColorLayout.from_wire("111111,222222,333333,444444"))

    def test_cycle_plus_minus_edit_spectrum_controls_not_rgb_draft(self):
        self.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        draft = self.state.draft
        self.assertEqual(handle_key(self.state, ord("+")), TuiCommand.NONE)
        self.assertEqual(self.state.cycle_from_deg, 5)
        self.assertEqual(handle_key(self.state, curses.KEY_DOWN), TuiCommand.NONE)
        self.assertEqual(handle_key(self.state, ord("-")), TuiCommand.NONE)
        self.assertEqual(self.state.cycle_to_deg, 355)
        self.assertEqual(handle_key(self.state, curses.KEY_DOWN), TuiCommand.NONE)
        self.assertEqual(handle_key(self.state, ord("-")), TuiCommand.NONE)
        self.assertEqual(self.state.cycle_saturation_percent, 85)
        self.assertEqual(self.state.draft, draft)
        self.assertEqual(self.state.current, draft)

    def test_cycle_preset_key_changes_spectrum_not_base_colors(self):
        self.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        draft = self.state.draft
        self.assertEqual(handle_key(self.state, ord("p")), TuiCommand.NONE)
        self.assertEqual((self.state.cycle_from_deg,
                          self.state.cycle_to_deg,
                          self.state.cycle_saturation_percent), (0, 80, 95))
        self.assertIn("Warm", self.state.message)
        self.assertEqual(self.state.draft, draft)
        self.assertEqual(handle_key(self.state, ord("p")), TuiCommand.NONE)
        self.assertEqual((self.state.cycle_from_deg,
                          self.state.cycle_to_deg,
                          self.state.cycle_saturation_percent), (170, 260, 90))
        self.assertEqual(self.state.draft, draft)

    def test_input_keys_map_to_physical_effect_zones_without_raw_device_access(self):
        self.assertEqual(effect_zone_for_key(ord("w"), Zone.CENTER), Zone.WASD)
        self.assertEqual(effect_zone_for_key(ord("q"), Zone.CENTER), Zone.LEFT)
        self.assertEqual(effect_zone_for_key(ord("`"), Zone.CENTER), Zone.LEFT)
        self.assertEqual(effect_zone_for_key(ord("v"), Zone.LEFT), Zone.CENTER)
        self.assertEqual(effect_zone_for_key(ord("g"), Zone.LEFT), Zone.CENTER)
        self.assertEqual(effect_zone_for_key(ord("j"), Zone.LEFT), Zone.CENTER)
        self.assertEqual(effect_zone_for_key(ord(";"), Zone.LEFT), Zone.CENTER)
        self.assertEqual(effect_zone_for_key(ord("]"), Zone.LEFT), Zone.CENTER)
        self.assertEqual(effect_zone_for_key(ord("'"), Zone.LEFT), Zone.CENTER)
        self.assertEqual(effect_zone_for_key(ord("["), Zone.WASD), Zone.RIGHT)
        self.assertEqual(effect_zone_for_key(9, Zone.WASD), Zone.LEFT)
        self.assertEqual(effect_zone_for_key(ord(" "), Zone.WASD), Zone.LEFT)
        self.assertEqual(effect_zone_for_key(10, Zone.WASD), Zone.RIGHT)
        self.assertEqual(effect_zone_for_key(curses.KEY_BACKSPACE, Zone.WASD), Zone.RIGHT)
        self.assertEqual(effect_zone_for_key(curses.KEY_LEFT, Zone.WASD), Zone.RIGHT)
        self.assertEqual(effect_zone_for_key(curses.KEY_F0 + 1, Zone.RIGHT), Zone.LEFT)
        self.assertEqual(effect_zone_for_key(curses.KEY_F0 + 5, Zone.RIGHT), Zone.LEFT)
        self.assertEqual(effect_zone_for_key(curses.KEY_F0 + 12, Zone.RIGHT), Zone.CENTER)
        self.assertEqual(effect_zone_for_key(curses.KEY_RIGHT, Zone.LEFT), Zone.RIGHT)
        self.assertEqual(effect_zone_for_key(0, Zone.CENTER), Zone.CENTER)

    def test_every_mapped_effect_input_uses_authoritative_keyboard_zone(self):
        for character, key_id in _CHARACTER_KEY_IDS.items():
            with self.subTest(character=repr(character), key_id=key_id):
                self.assertEqual(
                    effect_zone_for_key(ord(character), Zone.WASD),
                    key_zone_by_id(key_id),
                )
        for key, key_id in _SPECIAL_KEY_IDS.items():
            with self.subTest(key=key, key_id=key_id):
                self.assertEqual(
                    effect_zone_for_key(key, Zone.WASD),
                    key_zone_by_id(key_id),
                )

    def test_action_keys_return_commands_without_touching_firmware(self):
        self.assertEqual(handle_key(self.state, ord("a")), TuiCommand.APPLY)
        self.assertEqual(handle_key(self.state, ord("o")), TuiCommand.RESTORE)
        self.assertEqual(handle_key(self.state, ord("r")), TuiCommand.REFRESH)
        self.assertEqual(handle_key(self.state, ord("e")), TuiCommand.EDIT)
        self.assertEqual(handle_key(self.state, ord("m")), TuiCommand.PROFILES)
        self.assertEqual(handle_key(self.state, ord("s")), TuiCommand.STOP)
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

    def test_active_preset_does_not_claim_it_needs_apply(self):
        state = TuiState(current=PRESETS[0][1], original=PRESETS[0][1])

        handle_key(state, ord("p"))

        self.assertFalse(state.dirty)
        self.assertNotIn("press A", state.message)
        self.assertIn("already active", state.message)

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

    def test_key_surface_palette_uses_zone_tint_not_white_full_block_fill(self):
        body_foreground, top_foreground, background = tui_module._key_surface_colors(
            "710FFA"
        )

        self.assertNotEqual(top_foreground, "FFFFFF")
        self.assertNotEqual(background, "710FFA")
        self.assertGreater(
            sum(int(body_foreground[offset : offset + 2], 16) for offset in (0, 2, 4)),
            sum(int(background[offset : offset + 2], 16) for offset in (0, 2, 4)),
        )

    def test_key_roles_use_separate_pairs_and_zone_swatches_keep_exact_color(self):
        backend = mock.Mock()
        backend.status.return_value = {
            "state": "on",
            "colors": ["710FFA", "710FFA", "710FFA", "0FFA36"],
            "original": ["710FFA", "710FFA", "710FFA", "0FFA36"],
        }
        app = CursesTui(backend)
        app.colors_enabled = True

        with mock.patch.object(curses, "COLORS", 256, create=True), mock.patch.object(
            curses, "COLOR_PAIRS", 32767, create=True
        ), mock.patch("okeylitctl.tui.curses.init_pair") as init_pair, mock.patch(
            "okeylitctl.tui.curses.color_pair", return_value=0
        ):
            app._reference_role_attr("key_right")
            key_call = init_pair.call_args
            app._reference_role_attr("swatch_right")
            swatch_call = init_pair.call_args

        self.assertEqual(key_call.args[0], 30)
        self.assertNotEqual(key_call.args[2], rgb_to_xterm_index("710FFA"))
        self.assertNotEqual(swatch_call.args[0], key_call.args[0])
        self.assertEqual(swatch_call.args[2], rgb_to_xterm_index("710FFA"))

    def test_profile_preview_color_pair_does_not_redefine_a_keycap(self):
        backend = mock.Mock()
        backend.status.return_value = {
            "state": "on",
            "colors": ["710FFA", "710FFA", "710FFA", "0FFA36"],
            "original": ["710FFA", "710FFA", "710FFA", "0FFA36"],
        }
        app = CursesTui(backend)
        app.colors_enabled = True

        with mock.patch.object(curses, "COLORS", 256, create=True), mock.patch.object(
            curses, "COLOR_PAIRS", 32767, create=True
        ), mock.patch("okeylitctl.tui.curses.init_pair") as init_pair, mock.patch(
            "okeylitctl.tui.curses.color_pair", return_value=0
        ):
            app._reference_role_attr("key_right")
            key_pair = init_pair.call_args.args[0]
            app._reference_role_attr("profile_color_right_FF0000")
            profile_call = init_pair.call_args

        self.assertNotEqual(profile_call.args[0], key_pair)
        self.assertEqual(profile_call.args[2], rgb_to_xterm_index("FF0000"))

    def test_profile_preview_keycap_uses_dark_body_tinted_top_and_unique_pairs(self):
        backend = mock.Mock()
        backend.status.return_value = {
            "state": "on",
            "colors": ["710FFA", "710FFA", "710FFA", "0FFA36"],
            "original": ["710FFA", "710FFA", "710FFA", "0FFA36"],
        }
        app = CursesTui(backend)
        app.colors_enabled = True
        _, top, body = tui_module._key_surface_colors("FF0000")
        with mock.patch.object(curses, "COLORS", 256, create=True), mock.patch.object(
            curses, "COLOR_PAIRS", 32767, create=True
        ), mock.patch("okeylitctl.tui.curses.init_pair") as init_pair, mock.patch(
            "okeylitctl.tui.curses.color_pair", return_value=0
        ):
            app._reference_role_attr("profile_key_right_top_FF0000")
            self.assertTrue(init_pair.called, "profile top must initialize its own color pair")
            top_call = init_pair.call_args
            app._reference_role_attr("profile_key_right_FF0000")
            body_call = init_pair.call_args
            app._reference_role_attr("profile_color_right_FF0000")
            swatch_call = init_pair.call_args
        self.assertEqual(top_call.args[1:], (
            rgb_to_xterm_index(top), rgb_to_xterm_index(body)
        ))
        self.assertEqual(body_call.args[2], rgb_to_xterm_index(body))
        self.assertEqual(swatch_call.args[2], rgb_to_xterm_index("FF0000"))
        self.assertEqual(len({top_call.args[0], body_call.args[0], swatch_call.args[0], 30, 34}), 5)

    def test_swatch_pair_falls_back_without_redefining_an_unavailable_pair(self):
        backend = mock.Mock()
        backend.status.return_value = {
            "state": "on",
            "colors": ["710FFA", "710FFA", "710FFA", "0FFA36"],
            "original": ["710FFA", "710FFA", "710FFA", "0FFA36"],
        }
        app = CursesTui(backend)
        app.colors_enabled = True
        with mock.patch.object(curses, "COLORS", 256, create=True), mock.patch.object(
            curses, "COLOR_PAIRS", 22, create=True
        ), mock.patch("okeylitctl.tui.curses.init_pair") as init_pair:
            self.assertEqual(app._reference_role_attr("swatch_right"), curses.A_DIM)
        init_pair.assert_not_called()

    def test_key_top_role_uses_zone_tint_and_distinct_pair(self):
        backend = mock.Mock()
        backend.status.return_value = {
            "state": "on",
            "colors": ["710FFA", "710FFA", "710FFA", "0FFA36"],
            "original": ["710FFA", "710FFA", "710FFA", "0FFA36"],
        }
        app = CursesTui(backend)
        app.colors_enabled = True
        _, top_foreground, background = tui_module._key_surface_colors("710FFA")

        with mock.patch.object(curses, "COLORS", 256, create=True), mock.patch.object(
            curses, "COLOR_PAIRS", 32767, create=True
        ), mock.patch("okeylitctl.tui.curses.init_pair") as init_pair, mock.patch(
            "okeylitctl.tui.curses.color_pair", return_value=0
        ):
            app._reference_role_attr("key_right_top_selected")

        init_pair.assert_called_once_with(
            34,
            rgb_to_xterm_index(top_foreground),
            rgb_to_xterm_index(background),
        )

    def test_rainbow_spectrum_role_uses_its_hue_as_foreground(self):
        backend = mock.Mock()
        backend.status.return_value = {
            "state": "on",
            "colors": ["710FFA", "710FFA", "710FFA", "0FFA36"],
            "original": ["710FFA", "710FFA", "710FFA", "0FFA36"],
        }
        app = CursesTui(backend)
        app.colors_enabled = True
        with mock.patch.object(curses, "COLORS", 256, create=True), mock.patch.object(
            curses, "COLOR_PAIRS", 32767, create=True
        ), mock.patch("okeylitctl.tui.curses.init_pair") as init_pair, mock.patch(
            "okeylitctl.tui.curses.color_pair", return_value=0
        ):
            app._reference_role_attr("spectrum_0_FF1919")
        init_pair.assert_called_once_with(
            50, rgb_to_xterm_index("FF1919"), curses.COLOR_BLACK
        )

    def test_spectrum_falls_back_uniformly_when_pairs_cannot_hold_all_hues(self):
        backend = mock.Mock()
        backend.status.return_value = {
            "state": "on",
            "colors": ["710FFA", "710FFA", "710FFA", "0FFA36"],
            "original": ["710FFA", "710FFA", "710FFA", "0FFA36"],
        }
        app = CursesTui(backend)
        app.colors_enabled = True
        with mock.patch.object(curses, "COLORS", 256, create=True), mock.patch.object(
            curses, "COLOR_PAIRS", 64, create=True
        ), mock.patch("okeylitctl.tui.curses.init_pair") as init_pair:
            self.assertEqual(app._reference_role_attr("spectrum_0_FF1919"), curses.A_BOLD)
            self.assertEqual(app._reference_role_attr("spectrum_23_FF1919"), curses.A_BOLD)
        init_pair.assert_not_called()

    def test_effect_strip_role_paints_verified_zone_color_not_teal(self):
        backend = mock.Mock()
        backend.status.return_value = {
            "state": "on",
            "colors": ["710FFA", "710FFA", "710FFA", "0FFA36"],
            "original": ["710FFA", "710FFA", "710FFA", "0FFA36"],
        }
        app = CursesTui(backend)
        app.colors_enabled = True
        with mock.patch.object(curses, "COLORS", 256, create=True), mock.patch.object(
            curses, "COLOR_PAIRS", 32767, create=True
        ), mock.patch("okeylitctl.tui.curses.init_pair") as init_pair, mock.patch(
            "okeylitctl.tui.curses.color_pair", return_value=0
        ):
            app._reference_role_attr("effect_color_right_710FFA")
        init_pair.assert_called_once_with(
            100, rgb_to_xterm_index("710FFA"), curses.COLOR_BLACK
        )

    def test_verified_effect_key_roles_use_frame_color_not_draft(self):
        backend = mock.Mock()
        backend.status.return_value = {
            "state": "on",
            "colors": ["710FFA", "710FFA", "710FFA", "0FFA36"],
            "original": ["710FFA", "710FFA", "710FFA", "0FFA36"],
        }
        app = CursesTui(backend)
        app.colors_enabled = True
        _, top_foreground, background = tui_module._key_surface_colors("AA0000")
        with mock.patch.object(curses, "COLORS", 256, create=True), mock.patch.object(
            curses, "COLOR_PAIRS", 32767, create=True
        ), mock.patch("okeylitctl.tui.curses.init_pair") as init_pair, mock.patch(
            "okeylitctl.tui.curses.color_pair", return_value=0
        ):
            app._reference_role_attr("key_right_AA0000_top_selected")
            init_pair.assert_called_once_with(
                34, rgb_to_xterm_index(top_foreground),
                rgb_to_xterm_index(background),
            )
        self.assertEqual(app._reference_role_attr("key_unavailable"), curses.A_DIM)

    def test_monochrome_keycaps_show_top_and_legend_without_reversing_every_body(self):
        backend = mock.Mock()
        backend.status.return_value = {
            "state": "on",
            "colors": ["710FFA", "710FFA", "710FFA", "0FFA36"],
            "original": ["710FFA", "710FFA", "710FFA", "0FFA36"],
        }
        app = CursesTui(backend)
        app.colors_enabled = False

        self.assertEqual(app._reference_role_attr("key_right_top"), curses.A_BOLD)
        self.assertEqual(app._reference_role_attr("key_right"), 0)
        self.assertEqual(
            app._reference_role_attr("key_right_selected"),
            curses.A_REVERSE | curses.A_BOLD,
        )

    def test_key_pair_falls_back_when_terminal_has_too_few_color_pairs(self):
        backend = mock.Mock()
        backend.status.return_value = {
            "state": "on",
            "colors": ["710FFA", "710FFA", "710FFA", "0FFA36"],
            "original": ["710FFA", "710FFA", "710FFA", "0FFA36"],
        }
        app = CursesTui(backend)
        app.colors_enabled = True

        with mock.patch.object(curses, "COLORS", 256, create=True), mock.patch.object(
            curses, "COLOR_PAIRS", 34, create=True
        ), mock.patch("okeylitctl.tui.curses.init_pair") as init_pair:
            attr = app._reference_role_attr("key_right_top")

        self.assertEqual(attr, curses.A_BOLD)
        init_pair.assert_not_called()


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
        self.status_value["colors"] = list(self.status_value["original"])


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

    def rename(self, old_name, new_name):
        self.calls.append(("rename", old_name, new_name))
        self.layouts[new_name] = self.layouts.pop(old_name)

    def delete(self, name):
        self.calls.append(("delete", name))
        del self.layouts[name]


class CasEffectBackend(FakeBackend):
    """A broker-like fake: even an ABA write advances the CAS token."""

    def __init__(self):
        super().__init__()
        self.revision = 0
        self.cas_writes = []

    def snapshot(self):
        return {**self.status_value, "token": str(self.revision)}

    def compare_and_write(self, token, power, expected, replacement):
        if (token != str(self.revision) or power != self.status_value["state"]
                or expected != ",".join(self.status_value["colors"])):
            raise ConflictError("definitive CAS conflict")
        self.revision += 1
        self.status_value["colors"] = replacement.split(",")
        self.cas_writes.append(replacement)
        return str(self.revision)


class TuiDeviceActionTests(unittest.TestCase):
    def test_interrupt_after_static_mutation_marks_live_unknown_before_reraising(self):
        class Interrupted(FakeBackend):
            def write_colors(self, value):
                self.writes.append(value)
                raise KeyboardInterrupt

            def restore(self):
                self.restores += 1
                raise KeyboardInterrupt

        for action in ("apply", "restore"):
            with self.subTest(action=action):
                backend = Interrupted()
                app = CursesTui(backend)
                app.state.set_selected_color("ABCDEF")
                with self.assertRaises(KeyboardInterrupt):
                    getattr(app, f"_{action}")()
                self.assertTrue(app.state.live_unknown)
                self.assertEqual(app.state.draft.right, "ABCDEF")
                self.assertEqual(app.state.original.right, "AAAAAA")
                app._apply()
                app._restore()
                self.assertLessEqual(len(backend.writes), 1)
                self.assertLessEqual(backend.restores, 1)

    def test_interrupt_during_static_restore_readback_retains_unknown_lock(self):
        class ReadbackInterrupt(FakeBackend):
            def status(self):
                if self.restores:
                    raise KeyboardInterrupt
                return self.status_value

        backend = ReadbackInterrupt()
        app = CursesTui(backend)
        app.state.set_selected_color("ABCDEF")
        with self.assertRaises(KeyboardInterrupt):
            app._restore()
        self.assertTrue(app.state.live_unknown)
        app._restore()
        self.assertEqual(backend.restores, 1)

    def test_definite_apply_conflict_does_not_enter_unknown_write_lock(self):
        class Refused(FakeBackend):
            def write_colors(self, value):
                raise ConflictError("no write attempted")

        backend = Refused()
        app = CursesTui(backend)
        app.state.set_selected_color("ABCDEF")
        app._apply()
        self.assertFalse(app.state.live_unknown)
        self.assertTrue(app.state.live_last_observed)
        self.assertIn("definite conflict", app.state.message)
        self.assertEqual(backend.writes, [])
        self.assertEqual(app.state.draft.right, "ABCDEF")

    def test_restore_power_off_after_write_keeps_draft_and_unknown_lock(self):
        class PowerOffOnReadback(FakeBackend):
            def restore(self):
                super().restore()
                self.status_value["state"] = "off"

            def status(self):
                return {key: list(value) if isinstance(value, list) else value
                        for key, value in self.status_value.items()}

        backend = PowerOffOnReadback()
        app = CursesTui(backend)
        app.state.set_selected_color("ABCDEF")
        draft, original, current = app.state.draft, app.state.original, app.state.current

        app._restore()

        self.assertEqual(backend.restores, 1)
        self.assertEqual(app.power_state, "off")
        self.assertTrue(app.state.live_unknown)
        self.assertEqual((app.state.draft, app.state.original, app.state.current),
                         (draft, original, current))
        self.assertTrue(app.state.dirty)
        for composed in (
            compose_editor(app.state, power_state=app.power_state),
            compose_adaptive_editor(app.state, width=110, height=30,
                                    power_state=app.power_state),
            compose_narrow_editor(app.state, width=78, height=24,
                                  power_state=app.power_state),
        ):
            visible = "\n".join(composed.lines)
            self.assertIn("DEVICE OFF", visible)
            self.assertNotIn("IN SYNC", visible)
            self.assertNotIn("DEVICE ON", visible)
            self.assertNotIn("LIVE  #111111", visible)
        app._restore()
        app._apply()
        self.assertEqual(backend.restores, 1)
        self.assertEqual(backend.writes, [])

    def test_static_restore_does_not_bypass_running_effect_lock(self):
        backend = FakeBackend(same_as_original=True)
        app = CursesTui(backend)
        app.state.set_selected_color("ABCDEF")
        draft = app.state.draft
        app.state.effect_running = True

        app._restore()

        self.assertEqual(backend.restores, 0)
        self.assertEqual(backend.writes, [])
        self.assertEqual(app.state.draft, draft)
        self.assertTrue(app.state.effect_running)
        self.assertIn("Stop", app.state.message)

    def test_restore_invalid_prewrite_status_fails_closed_without_write(self):
        for invalid in ("sleep", None):
            with self.subTest(invalid=invalid):
                backend = FakeBackend(same_as_original=True)
                app = CursesTui(backend)
                app.state.set_selected_color("ABCDEF")
                draft, original = app.state.draft, app.state.original
                if invalid is None:
                    backend.status_value.pop("state")
                else:
                    backend.status_value["state"] = invalid

                app._restore()

                self.assertEqual(backend.restores, 0)
                self.assertEqual(backend.writes, [])
                self.assertEqual((app.state.draft, app.state.original), (draft, original))
                self.assertTrue(app.state.live_unknown)
                self.assertEqual(app.power_state, "unknown")
                visible = "\n".join(compose_editor(app.state, power_state=app.power_state).lines)
                self.assertIn("LIVE UNKNOWN", visible)
                self.assertNotIn("DEVICE ON", visible)
                self.assertNotIn("IN SYNC", visible)

    def test_restore_invalid_readback_status_keeps_unknown_and_never_retries(self):
        for invalid in ("sleep", None):
            with self.subTest(invalid=invalid):
                class InvalidReadback(FakeBackend):
                    def restore(self):
                        super().restore()
                        if invalid is None:
                            self.status_value.pop("state")
                        else:
                            self.status_value["state"] = invalid

                backend = InvalidReadback()
                app = CursesTui(backend)
                app.state.set_selected_color("ABCDEF")
                draft, original = app.state.draft, app.state.original

                app._restore()

                self.assertEqual(backend.restores, 1)
                self.assertEqual((app.state.draft, app.state.original), (draft, original))
                self.assertTrue(app.state.live_unknown)
                self.assertEqual(app.power_state, "unknown")
                app._restore()
                self.assertEqual(backend.restores, 1)
                visible = "\n".join(compose_editor(app.state, power_state=app.power_state).lines)
                self.assertIn("LIVE UNKNOWN", visible)
                self.assertNotIn("DEVICE ON", visible)
                self.assertNotIn("IN SYNC", visible)

    def test_restore_readback_failure_after_write_locks_as_ambiguous(self):
        class ReadbackFails(FakeBackend):
            def __init__(self):
                super().__init__()
                self.fail = False

            def restore(self):
                super().restore()
                self.fail = True

            def status(self):
                if self.fail:
                    raise BackendIOError("readback timed out")
                return self.status_value

        backend = ReadbackFails()
        app = CursesTui(backend)
        app.state.set_selected_color("ABCDEF")
        original, draft = app.state.original, app.state.draft
        app._restore()
        self.assertTrue(app.state.live_unknown)
        self.assertEqual((app.state.original, app.state.draft), (original, draft))
        app._restore()
        self.assertEqual(backend.restores, 1)

    def test_unknown_verification_detects_mutating_status_snapshot(self):
        class MutatingRead(FakeBackend):
            def __init__(self):
                super().__init__()
                self.reads = 0

            def status(self):
                self.reads += 1
                if self.reads == 3:
                    self.status_value["colors"][0] = "999999"
                return self.status_value

            def write_colors(self, value):
                raise BackendIOError("unacknowledged")

        backend = MutatingRead()
        app = CursesTui(backend)
        app.state.set_selected_color("ABCDEF")
        app._apply()
        app._refresh()
        self.assertTrue(app.state.live_unknown)
        self.assertIn("observation changed", app.state.message)

    def test_unknown_and_power_off_are_honest_in_profiles_and_hex_modal(self):
        app = CursesTui(FakeBackend(same_as_original=True))
        for unknown, power in ((True, "on"), (False, "off")):
            with self.subTest(unknown=unknown, power=power):
                app.state.live_unknown = unknown
                screens = (
                    compose_profiles(app.state, names=(), selected=0, preview_layout=None,
                                     power_state=power),
                    compose_responsive_profiles(app.state, width=110, height=30,
                                                names=(), selected=0, preview_layout=None,
                                                power_state=power),
                    compose_exact_hex_modal(app.state, power_state=power,
                                            input_text="ABCDEF", error=""),
                    compose_exact_hex_modal(app.state, width=110, height=30,
                                            power_state=power, input_text="ABCDEF", error=""),
                )
                for screen in screens:
                    visible = "\n".join(screen.lines)
                    self.assertIn("LIVE UNKNOWN" if unknown else "POWER OFF", visible)
                    self.assertNotIn("IN SYNC", visible)
                    self.assertFalse(any("modal_swatch_live_" in role for row in screen.roles
                                         for role in row))
                self.assertNotIn("#111111", "\n".join(screens[-1].lines))

    def test_synchronized_power_off_is_not_in_sync_in_any_editor(self):
        backend = FakeBackend(same_as_original=True)
        backend.status_value["state"] = "off"
        app = CursesTui(backend)
        for screen in (
            compose_editor(app.state, power_state="off"),
            compose_adaptive_editor(app.state, width=110, height=30, power_state="off"),
            compose_narrow_editor(app.state, width=78, height=24, power_state="off"),
        ):
            visible = "\n".join(screen.lines)
            self.assertIn("POWER OFF", visible)
            self.assertNotIn("IN SYNC", visible)
            self.assertNotIn("LIVE  #111111", visible)
            self.assertFalse(any("swatch_live_" in role or role.startswith("key_right_")
                                 for row in screen.roles for role in row))

    def test_ambiguous_apply_locks_all_writes_until_explicit_read_only_refresh(self):
        class AcceptedWithoutAck(FakeBackend):
            def write_colors(self, value):
                super().write_colors(value)
                self.status_value["colors"] = value.split(",")
                raise BackendIOError("ack lost")

        backend = AcceptedWithoutAck()
        app = CursesTui(backend, profile_store=FakeProfileStore())
        app.state.set_selected_color("ABCDEF")
        draft, original, stale = app.state.draft, app.state.original, app.state.current
        app._apply()
        self.assertEqual(backend.writes, [draft.to_wire()])
        self.assertEqual((app.state.draft, app.state.original, app.state.current),
                         (draft, original, stale))
        self.assertTrue(app.state.live_unknown)
        for composed in (
            compose_editor(app.state, power_state="on"),
            compose_adaptive_editor(app.state, width=110, height=30, power_state="on"),
            compose_narrow_editor(app.state, width=78, height=24, power_state="on"),
        ):
            visible = "\n".join(composed.lines)
            self.assertIn("LIVE UNKNOWN", visible)
            self.assertIn("R Verify", visible)
            self.assertNotIn("IN SYNC", visible)
            self.assertNotIn("#111111", visible)
            self.assertFalse(any("swatch_live_" in role for row in composed.roles for role in row))
        app._apply()
        app._restore()
        app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        self.assertFalse(app._start_effect(now=0.0))
        self.assertEqual(backend.writes, [draft.to_wire()])
        self.assertEqual(backend.restores, 0)
        self.assertEqual(app.state.draft, draft)
        app._refresh()
        self.assertFalse(app.state.live_unknown)
        self.assertEqual(app.state.current, draft)
        self.assertEqual(app.state.draft, draft)
        self.assertEqual(app.state.original, original)
        self.assertEqual(len(backend.writes), 1)

    def test_ambiguous_restore_retains_draft_and_original_through_failed_verification(self):
        class AcceptedRestore(FakeBackend):
            def __init__(self):
                super().__init__()
                self.fail_reads = False

            def restore(self):
                super().restore()
                raise BackendIOError("ack lost")

            def status(self):
                if self.fail_reads:
                    raise BackendIOError("read unavailable")
                return {key: list(value) if isinstance(value, list) else value
                        for key, value in self.status_value.items()}

        backend = AcceptedRestore()
        app = CursesTui(backend)
        app.state.set_selected_color("ABCDEF")
        draft, original, stale = app.state.draft, app.state.original, app.state.current
        app._restore()
        self.assertTrue(app.state.live_unknown)
        self.assertEqual((app.state.draft, app.state.original, app.state.current),
                         (draft, original, stale))
        backend.fail_reads = True
        app._refresh()
        self.assertTrue(app.state.live_unknown)
        app._restore()
        app._apply()
        self.assertEqual(backend.restores, 1)
        self.assertEqual(backend.writes, [])
        backend.fail_reads = False
        backend.status_value["state"] = "off"
        app._refresh()
        self.assertTrue(app.state.live_unknown)
        self.assertEqual(app.power_state, "off")
        self.assertEqual(app.state.draft, draft)
        backend.status_value["state"] = "on"
        app._refresh()
        self.assertFalse(app.state.live_unknown)
        self.assertEqual(app.state.current, original)
        self.assertEqual(app.state.draft, draft)
        self.assertEqual(backend.restores, 1)

    def test_confirmed_power_off_never_looks_illuminated_without_effect(self):
        backend = FakeBackend(same_as_original=True)
        backend.status_value["state"] = "off"
        app = CursesTui(backend)
        app.state.set_selected_color("ABCDEF")
        screens = (
            compose_editor(app.state, power_state="off"),
            compose_adaptive_editor(app.state, width=110, height=30, power_state="off"),
            compose_narrow_editor(app.state, width=78, height=24, power_state="off"),
        )
        for screen in screens:
            visible = "\n".join(screen.lines)
            self.assertIn("POWER OFF", visible)
            self.assertNotIn("IN SYNC", visible)
            self.assertNotIn("LIVE  #111111", visible)
            self.assertNotIn("#111111", visible)
            self.assertFalse(any("swatch_live_" in role or role.startswith("key_right_")
                                 for row in screen.roles for role in row))
            self.assertIn("#ABCDEF", visible)  # Clearly local DRAFT only.

    def test_cas_effect_power_off_never_offers_restore_of_lost_token(self):
        for action in ("tick", "stop"):
            with self.subTest(action=action):
                backend = CasEffectBackend()
                app = CursesTui(backend, profile_store=FakeProfileStore())
                app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
                app._start_effect(now=0.0)
                self.assertTrue(app._tick_effect(now=0.0, authorized=True))
                writes = list(backend.cas_writes)
                backend.status_value["state"] = "off"
                if action == "tick":
                    self.assertFalse(app._tick_effect(
                        now=app.effect_runtime.next_due, authorized=True
                    ))
                else:
                    self.assertFalse(app._stop_effect())
                self.assertEqual(backend.cas_writes, writes)
                self.assertEqual(app.power_state, "off")
                self.assertTrue(app.state.effect_running)
                self.assertFalse(app.state.effect_restore_pending)
                for composed in (
                    compose_editor(app.state, power_state="off"),
                    compose_adaptive_editor(app.state, width=110, height=30, power_state="off"),
                    compose_narrow_editor(app.state, width=78, height=24, power_state="off"),
                ):
                    visible = "\n".join(composed.lines)
                    self.assertNotIn("RESTORE PENDING", visible)
                    self.assertNotIn("S Restore", visible)
                    self.assertIn("POWER OFF", visible)
                backend.status_value["state"] = "on"
                backend.compare_and_write(str(backend.revision), "on", writes[-1],
                                          "AA0000,00BB00,0000CC,DDDD00")
                observed = list(backend.cas_writes)
                self.assertTrue(app._stop_effect())
                self.assertEqual(backend.cas_writes, observed)
                self.assertIsNone(app.effect_runtime)
                self.assertEqual(app.state.current.to_wire(), observed[-1])

    def test_external_cas_write_reconciles_observed_live_and_unlocks_without_restore(self):
        for action in ("tick", "stop"):
            for aba in (False, True):
                with self.subTest(action=action, aba=aba):
                    backend = CasEffectBackend()
                    app = CursesTui(backend, profile_store=FakeProfileStore())
                    app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
                    app._start_effect(now=0.0)
                    self.assertTrue(app._tick_effect(now=0.0, authorized=True))
                    frame = backend.cas_writes[-1]
                    base = app.state.draft.to_wire()
                    self.assertNotEqual(frame, base)
                    external = frame if aba else "AA0000,00BB00,0000CC,DDDD00"
                    backend.compare_and_write(str(backend.revision), "on", frame, external)
                    writes = list(backend.cas_writes)

                    if action == "tick":
                        self.assertFalse(app._tick_effect(
                            now=app.effect_runtime.next_due, authorized=True
                        ))
                    else:
                        self.assertTrue(app._stop_effect())

                    self.assertEqual(backend.cas_writes, writes)
                    self.assertIsNone(app.effect_runtime)
                    self.assertFalse(app.state.effect_running)
                    self.assertFalse(app.state.effect_restore_pending)
                    self.assertFalse(app.state.effect_frame_uncertain)
                    self.assertIsNone(app.state.effect_frame)
                    self.assertEqual(app.state.current.to_wire(), external)
                    self.assertEqual(app.state.draft.to_wire(), base)
                    self.assertTrue(app.state.dirty)
                    self.assertIn("ownership", app.state.message.lower())
                    for composed in (
                        compose_editor(app.state, power_state=app.power_state),
                        compose_adaptive_editor(app.state, width=110, height=30,
                                                power_state=app.power_state),
                        compose_narrow_editor(app.state, width=78, height=24,
                                              power_state=app.power_state),
                    ):
                        visible = "\n".join(composed.lines)
                        self.assertNotIn("IN SYNC", visible)
                        self.assertNotIn("RESTORE PENDING", visible)
                        self.assertNotIn("S Restore", visible)
                        self.assertNotIn("EFFECT ACTIVE", visible)
                        self.assertIn("UNSAVED DRAFT", visible)
                        self.assertIn(f"#{external.split(',')[0]}", visible)
                    app._stop_effect()
                    self.assertEqual(backend.cas_writes, writes)

    def test_lost_ownership_snapshot_race_stays_unknown_until_explicit_verification(self):
        class RacingBackend(CasEffectBackend):
            def __init__(self):
                super().__init__()
                self.race_on_reconcile = False
                self.reads_since_armed = 0

            def snapshot(self):
                snapshot = super().snapshot()
                if self.race_on_reconcile:
                    self.reads_since_armed += 1
                    if self.reads_since_armed == 2:
                        self.race_on_reconcile = False
                        self.compare_and_write(str(self.revision), "on",
                                               ",".join(self.status_value["colors"]),
                                               "BB0000,00CC00,0000DD,EEEE00")
                return snapshot

        backend = RacingBackend()
        app = CursesTui(backend, profile_store=FakeProfileStore())
        app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        app._start_effect(now=0.0)
        self.assertTrue(app._tick_effect(now=0.0, authorized=True))
        frame = backend.cas_writes[-1]
        backend.compare_and_write(str(backend.revision), "on", frame,
                                  "AA0000,00BB00,0000CC,DDDD00")
        backend.race_on_reconcile = True
        writes = list(backend.cas_writes)
        self.assertFalse(app._tick_effect(now=app.effect_runtime.next_due, authorized=True))
        self.assertEqual(backend.cas_writes, writes + ["BB0000,00CC00,0000DD,EEEE00"])
        self.assertIsNotNone(app.effect_runtime)
        self.assertTrue(app.state.effect_frame_uncertain)
        self.assertFalse(app.state.effect_restore_pending)
        for composed in (
            compose_editor(app.state, power_state="on"),
            compose_adaptive_editor(app.state, width=110, height=30, power_state="on"),
            compose_narrow_editor(app.state, width=78, height=24, power_state="on"),
        ):
            visible = "\n".join(composed.lines)
            self.assertIn("EFFECT UNKNOWN", visible)
            self.assertIn("S Verify", visible)
            self.assertNotIn("S Restore", visible)
            self.assertNotIn("#AA0000", visible)
        self.assertTrue(app._stop_effect())
        self.assertEqual(backend.cas_writes[-1], "BB0000,00CC00,0000DD,EEEE00")
        self.assertIsNone(app.effect_runtime)
        self.assertEqual(app.state.current.to_wire(), backend.cas_writes[-1])

    def test_lost_ownership_snapshot_error_retains_lock_until_explicit_verify(self):
        class TransientBackend(CasEffectBackend):
            def __init__(self):
                super().__init__()
                self.fail_next_snapshot = False
                self.reads_since_armed = 0

            def snapshot(self):
                if self.fail_next_snapshot:
                    self.reads_since_armed += 1
                    if self.reads_since_armed == 2:
                        self.fail_next_snapshot = False
                        raise BackendIOError("transient snapshot failure")
                return super().snapshot()

        backend = TransientBackend()
        app = CursesTui(backend, profile_store=FakeProfileStore())
        app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        app._start_effect(now=0.0)
        self.assertTrue(app._tick_effect(now=0.0, authorized=True))
        frame = backend.cas_writes[-1]
        backend.compare_and_write(str(backend.revision), "on", frame,
                                  "AA0000,00BB00,0000CC,DDDD00")
        backend.fail_next_snapshot = True
        self.assertFalse(app._stop_effect())
        self.assertTrue(app.state.effect_running)
        self.assertTrue(app.state.effect_frame_uncertain)
        self.assertFalse(app.state.effect_restore_pending)
        self.assertIn("S Verify", "\n".join(compose_editor(app.state, power_state="on").lines))
        self.assertTrue(app._stop_effect())
        self.assertIsNone(app.effect_runtime)
        self.assertEqual(len(backend.cas_writes), 2)

    def test_loss_reconciliation_labels_even_matching_draft_as_last_observed(self):
        class PostSnapshotWrite(CasEffectBackend):
            def __init__(self):
                super().__init__()
                self.after_second_snapshot = False
                self.reconcile_reads = 0

            def snapshot(self):
                observed = super().snapshot()
                if self.after_second_snapshot:
                    self.reconcile_reads += 1
                    if self.reconcile_reads == 3:
                        self.compare_and_write(str(self.revision), "on",
                                               ",".join(self.status_value["colors"]),
                                               "BB0000,00CC00,0000DD,EEEE00")
                return observed

        backend = PostSnapshotWrite()
        app = CursesTui(backend, profile_store=FakeProfileStore())
        app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        base = app.state.draft.to_wire()
        app._start_effect(now=0.0)
        app._tick_effect(now=0.0, authorized=True)
        backend.compare_and_write(str(backend.revision), "on", backend.cas_writes[-1], base)
        backend.after_second_snapshot = True
        self.assertTrue(app._stop_effect())
        self.assertFalse(app.state.dirty)
        self.assertEqual(app.state.current.to_wire(), base)
        self.assertEqual(backend.cas_writes[-1], "BB0000,00CC00,0000DD,EEEE00")
        for composed in (
            compose_editor(app.state, power_state="on"),
            compose_adaptive_editor(app.state, width=110, height=30, power_state="on"),
            compose_narrow_editor(app.state, width=78, height=24, power_state="on"),
        ):
            visible = "\n".join(composed.lines)
            self.assertIn("LAST OBSERVED", visible)
            self.assertNotIn("IN SYNC", visible)
            self.assertNotIn("S Restore", visible)
            self.assertIn("SEEN" if composed.width != 78 else "LAST OBSERVED #", visible)
            self.assertNotIn("LIVE  #111111", visible)

    def test_lost_ownership_cleanup_backs_off_without_retrying_base(self):
        class FailingReconciliationBackend(CasEffectBackend):
            def __init__(self):
                super().__init__()
                self.fail_reconciliation = False
                self.snapshot_attempts = 0

            def snapshot(self):
                if self.fail_reconciliation:
                    self.snapshot_attempts += 1
                    raise BackendIOError("reconciliation temporarily unavailable")
                return super().snapshot()

        backend = FailingReconciliationBackend()
        app = CursesTui(backend, profile_store=FakeProfileStore())
        app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        app._start_effect(now=0.0)
        self.assertTrue(app._tick_effect(now=0.0, authorized=True))
        frame = backend.cas_writes[-1]
        backend.compare_and_write(str(backend.revision), "on", frame,
                                  "AA0000,00BB00,0000CC,DDDD00")
        # Detect the definite conflict, but fail only the subsequent observation.
        original_snapshot = backend.snapshot
        def detect_then_fail():
            observed = original_snapshot()
            backend.snapshot = original_snapshot
            backend.fail_reconciliation = True
            return observed
        backend.snapshot = detect_then_fail
        self.assertFalse(app._tick_effect(now=app.effect_runtime.next_due, authorized=True))
        self.assertTrue(app.effect_runtime.ownership_lost)
        backend.snapshot_attempts = 0
        writes = list(backend.cas_writes)
        with mock.patch("okeylitctl.tui.sleep", side_effect=(None, None, KeyboardInterrupt)) as sleeper:
            self.assertFalse(app._cleanup_after_terminal_failure())
        self.assertEqual([call.args[0] for call in sleeper.call_args_list], [0.05, 0.1, 0.2])
        self.assertEqual(backend.snapshot_attempts, 3)
        self.assertEqual(backend.cas_writes, writes)
        self.assertTrue(app.state.effect_running)
        self.assertFalse(app.state.effect_restore_pending)
        self.assertTrue(app._terminal_cleanup_forced)

    def test_deliberate_apply_after_loss_clears_last_observed_label(self):
        backend = CasEffectBackend()
        app = CursesTui(backend, profile_store=FakeProfileStore())
        app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        app._start_effect(now=0.0)
        app._tick_effect(now=0.0, authorized=True)
        backend.compare_and_write(str(backend.revision), "on", backend.cas_writes[-1],
                                  "AA0000,00BB00,0000CC,DDDD00")
        self.assertTrue(app._stop_effect())
        self.assertTrue(app.state.live_last_observed)
        app.state.effect_index = tuple(EffectKind).index(EffectKind.STATIC)
        app._apply()
        self.assertFalse(app.state.live_last_observed)
        self.assertIn("IN SYNC", compose_editor(app.state, power_state="on").lines[1])

    def test_refresh_after_lost_ownership_keeps_provenance_and_local_draft(self):
        backend = CasEffectBackend()
        app = CursesTui(backend, profile_store=FakeProfileStore())
        app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        app.state.set_selected_color("ABCDEF")
        draft = app.state.draft
        app._start_effect(now=0.0)
        app._tick_effect(now=0.0, authorized=True)
        backend.compare_and_write(str(backend.revision), "on", backend.cas_writes[-1],
                                  draft.to_wire())
        self.assertTrue(app._stop_effect())
        self.assertFalse(app.state.dirty)
        self.assertTrue(app.state.live_last_observed)

        app._refresh()
        self.assertIs(app.state.draft, draft)
        self.assertTrue(app.state.live_last_observed)
        self.assertFalse(app.state.live_unknown)
        visible = "\n".join(compose_editor(app.state, power_state="on").lines)
        self.assertIn("LAST OBSERVED", visible)
        self.assertNotIn("IN SYNC", visible)
        self.assertEqual(backend.cas_writes.count(draft.to_wire()), 1)

    def test_refresh_after_lost_ownership_rejects_racing_token_and_power_off(self):
        class Racing(CasEffectBackend):
            race = False
            def snapshot(self):
                observed = super().snapshot()
                if self.race:
                    self.race = False
                    self.compare_and_write(str(self.revision), "on",
                                           ",".join(self.status_value["colors"]),
                                           "BB0000,00CC00,0000DD,EEEE00")
                return observed

        backend = Racing()
        app = CursesTui(backend, profile_store=FakeProfileStore())
        app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        app._start_effect(now=0.0)
        app._tick_effect(now=0.0, authorized=True)
        backend.compare_and_write(str(backend.revision), "on", backend.cas_writes[-1],
                                  "AA0000,00BB00,0000CC,DDDD00")
        self.assertTrue(app._stop_effect())
        app.state.set_selected_color("ABCDEF")
        draft = app.state.draft
        current = app.state.current
        backend.race = True
        app._refresh()
        self.assertTrue(app.state.live_unknown)
        self.assertTrue(app.state.live_last_observed)
        self.assertEqual((app.state.draft, app.state.current), (draft, current))
        self.assertNotIn("IN SYNC", "\n".join(compose_editor(app.state, power_state="on").lines))
        backend.status_value["state"] = "off"
        app._refresh()
        self.assertEqual(app.power_state, "off")
        self.assertTrue(app.state.live_unknown)
        self.assertEqual(app.state.draft, draft)

    def test_lost_ownership_reconciliation_requires_token_bearing_observations(self):
        backend = CasEffectBackend()
        app = CursesTui(backend, profile_store=FakeProfileStore())
        app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        app.state.set_selected_color("ABCDEF")
        draft = app.state.draft
        app._start_effect(now=0.0)
        app._tick_effect(now=0.0, authorized=True)
        backend.compare_and_write(str(backend.revision), "on", backend.cas_writes[-1],
                                  draft.to_wire())
        actual_snapshot = backend.snapshot
        backend.snapshot = lambda: {k: v for k, v in actual_snapshot().items()
                                    if k != "token"}
        self.assertFalse(app._stop_effect())
        self.assertIsNotNone(app.effect_runtime)
        self.assertTrue(app.state.effect_frame_uncertain)
        self.assertFalse(app.state.live_last_observed)
        self.assertEqual(app.state.draft, draft)
        backend.snapshot = actual_snapshot
        self.assertTrue(app._stop_effect())
        self.assertTrue(app.state.live_last_observed)
        self.assertEqual(backend.cas_writes[-1], draft.to_wire())

    def test_repeated_legacy_refresh_after_ambiguous_apply_remains_last_observed(self):
        class AcceptedWithoutAck(FakeBackend):
            def write_colors(self, value):
                self.writes.append(value)
                self.status_value["colors"] = value.split(",")
                raise BackendIOError("ack lost")

        backend = AcceptedWithoutAck()
        app = CursesTui(backend)
        app.state.set_selected_color("ABCDEF")
        draft = app.state.draft
        app._apply()
        app._refresh()
        self.assertTrue(app.state.live_last_observed)
        app._refresh()
        self.assertFalse(app.state.live_unknown)
        self.assertTrue(app.state.live_last_observed)
        self.assertEqual(app.state.draft, draft)
        self.assertNotIn("IN SYNC", "\n".join(compose_editor(app.state, power_state="on").lines))

    def test_nonstatic_effect_start_tick_and_stop_restore_the_chosen_base(self):
        backend = FakeBackend()
        app = CursesTui(backend, profile_store=FakeProfileStore())
        app.state.set_selected_color("A1B2C3")
        app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        chosen_base = app.state.draft

        self.assertTrue(app._start_effect(now=0.0))
        self.assertEqual(backend.writes, [])
        self.assertTrue(app.state.effect_running)

        self.assertTrue(app._tick_effect(now=0.0, authorized=True))
        self.assertIsNotNone(app.effect_runtime)
        self.assertEqual(app.state.effect_frame, app.effect_runtime.last_written)
        self.assertNotEqual(app.state.effect_frame, chosen_base)
        self.assertNotEqual(backend.writes[-1], chosen_base.to_wire())

        self.assertTrue(app._stop_effect())
        self.assertIsNone(app.state.effect_frame)
        self.assertEqual(backend.writes[-1], chosen_base.to_wire())
        self.assertEqual(app.state.current, chosen_base)
        self.assertFalse(app.state.effect_running)
        self.assertFalse(app.state.dirty)

    def test_first_uncertain_write_retains_runtime_and_blocks_quit_until_resolved(self):
        class WriteThenFailReadbackBackend(FakeBackend):
            def __init__(self):
                super().__init__()
                self.fail_once = True

            def write_colors(self, value):
                self.writes.append(value)
                self.status_value["colors"] = value.split(",")
                if self.fail_once:
                    self.fail_once = False
                    raise BackendIOError("injected readback failure")

        backend = WriteThenFailReadbackBackend()
        app = CursesTui(backend, profile_store=FakeProfileStore())
        app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)

        app._start_effect(now=0.0)
        self.assertFalse(app._tick_effect(now=0.0, authorized=True))

        self.assertIsNotNone(app.effect_runtime)
        self.assertTrue(app.effect_runtime.write_state_uncertain)
        self.assertTrue(app.state.effect_running)
        self.assertTrue(app.state.effect_frame_uncertain)
        self.assertNotEqual(backend.status_value["colors"], app.effect_runtime.spec.base.to_wire().split(","))
        self.assertFalse(app._stop_effect())
        self.assertTrue(app.state.effect_running)
        self.assertFalse(app.state.effect_frame_uncertain)
        self.assertEqual(app.state.effect_frame, app.effect_runtime.last_written)
        self.assertEqual(app.state.effect_frame.to_wire().split(","), backend.status_value["colors"])
        self.assertIn("press s again", app.state.message.lower())
        self.assertTrue(app._stop_effect())
        self.assertFalse(app.state.effect_running)
        self.assertEqual(backend.status_value["colors"], app.state.current.to_wire().split(","))

    def test_firmware_error_after_success_keeps_restore_pending_and_editor_locked(self):
        class FailingStatusBackend(FakeBackend):
            def __init__(self):
                super().__init__()
                self.fail_status = False

            def status(self):
                if self.fail_status:
                    raise BackendIOError("injected status failure")
                return super().status()

        backend = FailingStatusBackend()
        app = CursesTui(backend, profile_store=FakeProfileStore())
        app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)

        app._start_effect(now=0.0)
        self.assertTrue(app._tick_effect(now=0.0, authorized=True))
        self.assertIsNotNone(app.effect_runtime)
        backend.fail_status = True

        self.assertFalse(
            app._tick_effect(now=app.effect_runtime.next_due, authorized=True)
        )

        self.assertIsNotNone(app.effect_runtime)
        self.assertTrue(app.state.effect_running)
        self.assertTrue(app.state.effect_restore_pending)
        self.assertTrue(app.state.effect_frame_uncertain)
        original_message = app.state.message
        self.assertIn("restoration is pending", original_message.lower())

        self.assertFalse(
            app._tick_effect(now=app.effect_runtime.next_due, authorized=True)
        )
        self.assertEqual(app.power_state, "on")
        self.assertEqual(app.state.message, original_message)
        self.assertTrue(app.state.effect_running)

    def test_verified_base_frame_is_reconciled_when_effect_stops(self):
        backend = FakeBackend()
        app = CursesTui(backend, profile_store=FakeProfileStore())
        app.state.set_selected_color("ABCDEF")
        chosen_base = app.state.draft
        app.state.effect_index = tuple(EffectKind).index(EffectKind.BLINK)
        app.state.effect_light = 1.0

        app._start_effect(now=0.0)
        self.assertTrue(app._tick_effect(now=0.0, authorized=True))
        self.assertEqual(backend.writes[-1], chosen_base.to_wire())
        self.assertTrue(app.state.dirty)

        self.assertTrue(app._stop_effect())
        self.assertEqual(app.state.current, chosen_base)
        self.assertFalse(app.state.dirty)
        self.assertIn("base layout active", app.state.message.lower())

    def test_power_off_keeps_effect_restore_pending_and_editor_locked(self):
        backend = FakeBackend()
        app = CursesTui(backend, profile_store=FakeProfileStore())
        app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        base = app.state.draft.to_wire()

        app._start_effect(now=0.0)
        self.assertTrue(app._tick_effect(now=0.0, authorized=True))
        backend.status_value["state"] = "off"
        self.assertIsNotNone(app.effect_runtime)
        self.assertFalse(
            app._tick_effect(now=app.effect_runtime.next_due, authorized=True)
        )

        writes_before_stop = list(backend.writes)
        self.assertTrue(app.state.effect_running)
        self.assertEqual(app.power_state, "off")
        self.assertFalse(app._stop_effect())
        self.assertEqual(backend.writes, writes_before_stop)
        self.assertIsNotNone(app.effect_runtime)
        self.assertTrue(app.state.effect_running)

        backend.status_value["state"] = "on"
        self.assertTrue(app._stop_effect())
        self.assertEqual(backend.writes[-1], base)
        self.assertIsNone(app.effect_runtime)
        self.assertFalse(app.state.effect_running)

    def test_stop_observing_power_off_updates_device_badge_without_writing(self):
        backend = FakeBackend()
        app = CursesTui(backend, profile_store=FakeProfileStore())
        app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        app._start_effect(now=0.0)
        app._tick_effect(now=0.0, authorized=True)
        writes_before_stop = list(backend.writes)
        backend.status_value["state"] = "off"

        self.assertFalse(app._stop_effect())

        self.assertEqual(app.power_state, "off")
        self.assertEqual(backend.writes, writes_before_stop)
        self.assertIsNotNone(app.effect_runtime)
        self.assertTrue(app.state.effect_running)

    def test_uncertain_stop_observing_power_off_updates_device_badge(self):
        class ReadbackFailureAfterWriteBackend(FakeBackend):
            def write_colors(self, value):
                self.writes.append(value)
                self.status_value["colors"] = value.split(",")
                raise BackendIOError("readback failed")

        backend = ReadbackFailureAfterWriteBackend()
        app = CursesTui(backend, profile_store=FakeProfileStore())
        app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        app._start_effect(now=0.0)
        self.assertFalse(app._tick_effect(now=0.0, authorized=True))
        writes_before_stop = list(backend.writes)
        backend.status_value["state"] = "off"

        self.assertFalse(app._stop_effect())

        self.assertEqual(app.power_state, "off")
        self.assertEqual(backend.writes, writes_before_stop)
        self.assertTrue(app.state.effect_running)
        self.assertTrue(app.state.effect_frame_uncertain)

    def test_reactive_tui_input_triggers_runtime_without_direct_device_access(self):
        backend = FakeBackend()
        app = CursesTui(backend, profile_store=FakeProfileStore())
        app.state.effect_index = tuple(EffectKind).index(EffectKind.REACTIVE)
        app._start_effect(now=0.0)

        self.assertTrue(app._trigger_effect_input(ord("w"), now=0.1))
        self.assertEqual(backend.writes, [])
        self.assertIsNotNone(app.effect_runtime)
        self.assertEqual(app.effect_runtime.event.zone, Zone.WASD)

        self.assertTrue(app._tick_effect(now=0.1, authorized=True))
        self.assertEqual(len(backend.writes), 1)

    def test_stop_before_first_frame_succeeds_without_firmware_write(self):
        backend = FakeBackend()
        app = CursesTui(backend, profile_store=FakeProfileStore())
        app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)

        self.assertTrue(app._start_effect(now=0.0))
        self.assertTrue(app._stop_effect())

        self.assertEqual(backend.writes, [])
        self.assertIsNone(app.effect_runtime)
        self.assertFalse(app.state.effect_running)

    def test_effect_preview_roles_remain_visible_in_monochrome(self):
        app = CursesTui(FakeBackend(), profile_store=FakeProfileStore())
        app.colors_enabled = False

        self.assertTrue(app._reference_role_attr("effect_preview_cycle") & curses.A_BOLD)
        self.assertTrue(app._reference_role_attr("effect_preview_rain") & curses.A_BOLD)

    def test_confirmation_temporarily_uses_blocking_input_then_restores_polling(self):
        app = CursesTui(FakeBackend(), profile_store=FakeProfileStore())

        class TimedConfirmScreen:
            def __init__(self):
                self.timeout_value = 50
                self.timeouts = []

            def timeout(self, value):
                self.timeout_value = value
                self.timeouts.append(value)

            def getmaxyx(self):
                return (30, 100)

            def addnstr(self, *_args):
                return None

            def refresh(self):
                return None

            def getch(self):
                return ord("y") if self.timeout_value == -1 else -1

        screen = TimedConfirmScreen()
        self.assertTrue(app._confirm_action(screen, "Restore?"))
        self.assertEqual(screen.timeouts, [-1, 50])

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

    def test_confirmation_fails_closed_if_terminal_resizes_while_yes_is_read(self):
        app = CursesTui(FakeBackend(), profile_store=FakeProfileStore())

        class ResizingConfirmationScreen:
            def __init__(self):
                self.width = 100

            def getmaxyx(self):
                return (30, self.width)

            def addnstr(self, *_args):
                return None

            def refresh(self):
                return None

            def getch(self):
                self.width = 10
                return ord("y")

        self.assertFalse(
            app._confirm_action(ResizingConfirmationScreen(), "Restore?")
        )

    def test_terminal_input_failure_restores_a_running_effect_base(self):
        backend = FakeBackend()
        app = CursesTui(backend, profile_store=FakeProfileStore())
        app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        base = app.state.draft.to_wire()
        app._start_effect(now=0.0)
        app._tick_effect(now=0.0, authorized=True)
        self.assertNotEqual(backend.writes[-1], base)

        class FailingInputScreen:
            def keypad(self, _enabled):
                return None

            def timeout(self, _milliseconds):
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

        with mock.patch("okeylitctl.tui.initialize_colors", return_value=False), mock.patch(
            "okeylitctl.tui.monotonic", return_value=0.1
        ):
            app._main(FailingInputScreen())

        self.assertEqual(backend.writes[-1], base)
        self.assertIsNone(app.effect_runtime)
        self.assertFalse(app.state.effect_running)

    def test_keyboard_interrupt_keeps_tui_alive_until_pending_restore_succeeds(self):
        class TransientCleanupFailureBackend(FakeBackend):
            def __init__(self):
                super().__init__()
                self.fail_next_status = False

            def status(self):
                if self.fail_next_status:
                    self.fail_next_status = False
                    raise BackendIOError("transient cleanup status failure")
                return super().status()

        backend = TransientCleanupFailureBackend()
        app = CursesTui(backend, profile_store=FakeProfileStore())
        app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        base = app.state.draft.to_wire()
        app._start_effect(now=0.0)
        app._tick_effect(now=0.0, authorized=True)
        backend.fail_next_status = True

        class InterruptingScreen:
            def __init__(self):
                self.reads = 0

            def keypad(self, _enabled):
                return None

            def timeout(self, _milliseconds):
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
                self.reads += 1
                raise KeyboardInterrupt

        screen = InterruptingScreen()
        with mock.patch("okeylitctl.tui.initialize_colors", return_value=False), mock.patch(
            "okeylitctl.tui.monotonic", return_value=0.1
        ):
            app._main(screen)

        self.assertEqual(screen.reads, 2)
        self.assertEqual(backend.writes[-1], base)
        self.assertIsNone(app.effect_runtime)
        self.assertFalse(app.state.effect_running)

    def test_interrupt_during_effect_write_resolves_then_restores_before_exit(self):
        class InterruptAfterWriteBackend(FakeBackend):
            def __init__(self):
                super().__init__()
                self.interrupt_once = True

            def write_colors(self, value):
                self.writes.append(value)
                self.status_value["colors"] = value.split(",")
                if self.interrupt_once:
                    self.interrupt_once = False
                    raise KeyboardInterrupt

        backend = InterruptAfterWriteBackend()
        app = CursesTui(backend, profile_store=FakeProfileStore())
        app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        base = app.state.draft.to_wire()
        app._start_effect(now=0.0)

        class InterruptingScreen:
            def __init__(self):
                self.reads = 0

            def keypad(self, _enabled):
                return None

            def timeout(self, _milliseconds):
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
                self.reads += 1
                raise KeyboardInterrupt

        screen = InterruptingScreen()
        with mock.patch("okeylitctl.tui.initialize_colors", return_value=False), mock.patch(
            "okeylitctl.tui.monotonic", return_value=0.0
        ):
            app._main(screen)

        self.assertEqual(screen.reads, 1)
        self.assertEqual(backend.status_value["colors"], base.split(","))
        self.assertEqual(backend.writes[-1], base)
        self.assertIsNone(app.effect_runtime)
        self.assertFalse(app.state.effect_running)

    def test_interrupt_after_restore_write_never_duplicates_base_write(self):
        class InterruptAfterRestoreBackend(FakeBackend):
            def __init__(self):
                super().__init__()
                self.interrupt_restore_once = True

            def write_colors(self, value):
                self.writes.append(value)
                self.status_value["colors"] = value.split(",")
                base = "111111,222222,333333,444444"
                if value == base and self.interrupt_restore_once:
                    self.interrupt_restore_once = False
                    raise KeyboardInterrupt

        backend = InterruptAfterRestoreBackend()
        app = CursesTui(backend, profile_store=FakeProfileStore())
        app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        base = app.state.draft.to_wire()
        app._start_effect(now=0.0)
        app._tick_effect(now=0.0, authorized=True)

        class StopScreen:
            def __init__(self):
                self.keys = iter((ord("s"),))

            def keypad(self, _enabled):
                return None

            def timeout(self, _milliseconds):
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
            app._main(StopScreen())

        self.assertEqual(backend.writes.count(base), 1)
        self.assertEqual(backend.status_value["colors"], base.split(","))
        self.assertIsNone(app.effect_runtime)
        self.assertFalse(app.state.effect_running)

    def test_main_interrupted_cas_stop_uses_cleanup_gate_without_duplicate_write(self):
        for accepted in (False, True):
            with self.subTest(accepted=accepted):
                class InterruptedStop(CasEffectBackend):
                    interrupt_once = True
                    def compare_and_write(self, token, power, expected, replacement):
                        if replacement == base and self.interrupt_once:
                            self.interrupt_once = False
                            if accepted:
                                super().compare_and_write(token, power, expected, replacement)
                            raise KeyboardInterrupt
                        return super().compare_and_write(token, power, expected, replacement)

                backend = InterruptedStop()
                app = CursesTui(backend, profile_store=FakeProfileStore())
                app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
                base = app.state.draft.to_wire()
                app._start_effect(now=0.0)
                app._tick_effect(now=0.0, authorized=True)
                frame = backend.cas_writes[-1]

                class StopScreen:
                    reads = 0
                    def keypad(self, _enabled): pass
                    def timeout(self, _milliseconds): pass
                    def getmaxyx(self): return (30, 110)
                    def erase(self): pass
                    def addnstr(self, *_args): pass
                    def refresh(self): pass
                    def getch(self):
                        self.reads += 1
                        if self.reads > 1:
                            raise AssertionError("cleanup must not resume input")
                        return ord("s")

                with mock.patch("okeylitctl.tui.initialize_colors", return_value=False), \
                     mock.patch("okeylitctl.tui.monotonic", return_value=0.1), \
                     mock.patch("okeylitctl.tui.sleep") as sleeper:
                    app._main(StopScreen())
                self.assertEqual(backend.cas_writes, [frame, base])
                if accepted:
                    self.assertTrue(app.state.live_last_observed)
                    self.assertIn("ownership lost", app.state.message.lower())
                    self.assertNotIn("restored", app.state.message.lower())
                else:
                    self.assertEqual(app.state.message, "Effect stopped; base layout restored")
                    self.assertEqual(sleeper.call_count, 1)
                self.assertIsNone(app.effect_runtime)
                self.assertFalse(app._terminal_cleanup_forced)

    def test_interrupted_cas_stop_power_off_bounds_cleanup_without_claiming_restore(self):
        class PowerOffStop(CasEffectBackend):
            interrupt_once = True
            def compare_and_write(self, token, power, expected, replacement):
                if replacement == base and self.interrupt_once:
                    self.interrupt_once = False
                    self.status_value["state"] = "off"
                    raise KeyboardInterrupt
                return super().compare_and_write(token, power, expected, replacement)

        backend = PowerOffStop()
        app = CursesTui(backend, profile_store=FakeProfileStore())
        app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        base = app.state.draft.to_wire()
        app._start_effect(now=0.0)
        app._tick_effect(now=0.0, authorized=True)
        frame = backend.cas_writes[-1]

        class StopScreen:
            def keypad(self, _enabled): pass
            def timeout(self, _milliseconds): pass
            def getmaxyx(self): return (30, 110)
            def erase(self): pass
            def addnstr(self, *_args): pass
            def refresh(self): pass
            def getch(self): return ord("s")

        with mock.patch("okeylitctl.tui.initialize_colors", return_value=False), \
             mock.patch("okeylitctl.tui.monotonic", return_value=0.1), \
             mock.patch("okeylitctl.tui.sleep") as sleeper:
            app._main(StopScreen())
        self.assertLessEqual(sleeper.call_count, 6)
        self.assertEqual(backend.cas_writes, [frame])
        self.assertEqual(app.power_state, "off")
        self.assertIsNotNone(app.effect_runtime)
        self.assertTrue(app.state.effect_frame_uncertain)
        self.assertTrue(app._terminal_cleanup_forced)
        self.assertIn("unresolved", app.state.message.lower())

    def test_terminal_failure_with_interrupted_cas_stop_resolves_without_double_write(self):
        class InterruptedStop(CasEffectBackend):
            interrupt_once = True
            def compare_and_write(self, token, power, expected, replacement):
                if replacement == base and self.interrupt_once:
                    self.interrupt_once = False
                    raise KeyboardInterrupt
                return super().compare_and_write(token, power, expected, replacement)

        backend = InterruptedStop()
        app = CursesTui(backend, profile_store=FakeProfileStore())
        app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        base = app.state.draft.to_wire()
        app._start_effect(now=0.0)
        app._tick_effect(now=0.0, authorized=True)

        class BrokenScreen:
            def keypad(self, _enabled): pass
            def timeout(self, _milliseconds): pass
            def getmaxyx(self): return (30, 110)
            def erase(self): pass
            def addnstr(self, *_args): pass
            def refresh(self): pass
            def getch(self): raise curses.error("terminal failed")

        with mock.patch("okeylitctl.tui.initialize_colors", return_value=False), \
             mock.patch("okeylitctl.tui.monotonic", return_value=0.1), \
             mock.patch("okeylitctl.tui.sleep"):
            app._main(BrokenScreen())
        self.assertEqual(backend.cas_writes.count(base), 1)
        self.assertIsNone(app.effect_runtime)

    def test_terminal_input_failure_waits_for_pending_power_off_restore(self):
        backend = FakeBackend()
        app = CursesTui(backend, profile_store=FakeProfileStore())
        app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        base = app.state.draft.to_wire()
        app._start_effect(now=0.0)
        app._tick_effect(now=0.0, authorized=True)
        backend.status_value["state"] = "off"

        class RecoveringInputScreen:
            def __init__(self):
                self.reads = 0

            def keypad(self, _enabled):
                return None

            def timeout(self, _milliseconds):
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
                self.reads += 1
                if self.reads == 2:
                    backend.status_value["state"] = "on"
                raise curses.error

        screen = RecoveringInputScreen()

        def restore_power(_delay):
            backend.status_value["state"] = "on"

        with mock.patch("okeylitctl.tui.initialize_colors", return_value=False), mock.patch(
            "okeylitctl.tui.monotonic", return_value=0.1
        ), mock.patch("okeylitctl.tui.sleep", side_effect=restore_power):
            app._main(screen)

        self.assertEqual(screen.reads, 1)
        self.assertEqual(backend.writes[-1], base)
        self.assertIsNone(app.effect_runtime)
        self.assertFalse(app.state.effect_running)

    def test_main_loop_routes_foreground_input_to_reactive_effect(self):
        backend = FakeBackend()
        app = CursesTui(backend, profile_store=FakeProfileStore())
        base = app.state.draft.to_wire()

        class ScriptedScreen:
            def __init__(self):
                self.keys = iter((9,) * 8 + (ord("a"), ord("w"), ord("s"), ord("q")))

            def keypad(self, _enabled):
                return None

            def timeout(self, _milliseconds):
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

        times = iter((0.0, 0.0, 0.1, 0.1, 0.1, 0.1))
        with mock.patch("okeylitctl.tui.initialize_colors", return_value=False), mock.patch(
            "okeylitctl.tui.monotonic", side_effect=lambda: next(times, 0.1)
        ):
            app._main(ScriptedScreen())

        self.assertGreaterEqual(len(backend.writes), 2)
        self.assertNotEqual(backend.writes[0], base)
        self.assertEqual(backend.writes[-1], base)
        self.assertFalse(app.state.effect_running)

    def test_running_effect_locks_editor_mutations_until_stop(self):
        backend = FakeBackend()
        app = CursesTui(backend, profile_store=FakeProfileStore())

        class ScriptedScreen:
            def __init__(self):
                self.keys = iter(
                    (9, 9, 9, 9, 9, ord("a"), 9, ord("+"), ord("s"), ord("q"))
                )

            def keypad(self, _enabled):
                return None

            def timeout(self, _milliseconds):
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

        with mock.patch("okeylitctl.tui.initialize_colors", return_value=False), mock.patch(
            "okeylitctl.tui.monotonic", return_value=0.0
        ):
            app._main(ScriptedScreen())

        self.assertEqual(app.state.effect_kind, EffectKind.CYCLE)
        self.assertEqual(app.state.current.to_wire(), "111111,222222,333333,444444")
        self.assertEqual(app.state.draft, app.state.current)

    def test_main_loop_runs_selected_effect_in_foreground_and_stops_to_base(self):
        backend = FakeBackend()
        app = CursesTui(backend, profile_store=FakeProfileStore())
        base = app.state.draft.to_wire()

        class ScriptedScreen:
            def __init__(self):
                self.keys = iter((9, 9, 9, 9, 9, ord("a"), ord("s"), ord("q")))

            def keypad(self, _enabled):
                return None

            def timeout(self, _milliseconds):
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

        with mock.patch("okeylitctl.tui.initialize_colors", return_value=False), mock.patch(
            "okeylitctl.tui.monotonic", return_value=0.0
        ):
            app._main(ScriptedScreen())

        self.assertGreaterEqual(len(backend.writes), 2)
        self.assertNotEqual(backend.writes[0], base)
        self.assertEqual(backend.writes[-1], base)
        self.assertFalse(app.state.effect_running)

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

        class RecoveringNarrowScreen(NarrowScreen):
            def getch(self):
                self.reads += 1
                key = next(self.keys)
                if key == curses.KEY_RESIZE:
                    self.width = 20
                return key

        too_small = RecoveringNarrowScreen(
            19, (ord("q"), curses.KEY_RESIZE, ord("q"), ord("y"))
        )
        with mock.patch("okeylitctl.tui.initialize_colors", return_value=False):
            app._main(too_small)
        self.assertEqual(too_small.reads, 4)

    def test_render_failure_retries_pending_effect_restoration_before_exit(self):
        class TransientCleanupFailureBackend(FakeBackend):
            def __init__(self):
                super().__init__()
                self.fail_next_status = False

            def status(self):
                if self.fail_next_status:
                    self.fail_next_status = False
                    raise BackendIOError("transient cleanup status failure")
                return super().status()

        backend = TransientCleanupFailureBackend()
        app = CursesTui(backend, profile_store=FakeProfileStore())
        app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        base = app.state.draft.to_wire()
        app._start_effect(now=0.0)
        app._tick_effect(now=0.0, authorized=True)
        backend.fail_next_status = True

        class FailingRenderScreen:
            def __init__(self):
                self.erases = 0

            def keypad(self, _enabled):
                return None

            def timeout(self, _milliseconds):
                return None

            def erase(self):
                self.erases += 1
                raise curses.error("render failed")

        screen = FailingRenderScreen()
        with mock.patch("okeylitctl.tui.initialize_colors", return_value=False):
            app._main(screen)

        self.assertEqual(screen.erases, 1)
        self.assertEqual(backend.writes[-1], base)
        self.assertIsNone(app.effect_runtime)
        self.assertFalse(app.state.effect_running)

    def test_terminal_cleanup_restores_after_uncertain_frame_is_verified(self):
        class ReadbackFailureAfterWriteBackend(FakeBackend):
            def __init__(self):
                super().__init__()
                self.fail_first_write = True

            def write_colors(self, value):
                self.writes.append(value)
                self.status_value["colors"] = value.split(",")
                if self.fail_first_write:
                    self.fail_first_write = False
                    raise BackendIOError("effect readback failed")

        backend = ReadbackFailureAfterWriteBackend()
        app = CursesTui(backend, profile_store=FakeProfileStore())
        app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        base = app.state.draft.to_wire()
        app._start_effect(now=0.0)
        self.assertFalse(app._tick_effect(now=0.0, authorized=True))
        self.assertTrue(app.state.effect_frame_uncertain)

        with mock.patch(
            "okeylitctl.tui.sleep", side_effect=(None, AssertionError("cleanup stalled"))
        ) as sleeper:
            self.assertTrue(app._cleanup_after_terminal_failure())

        self.assertEqual(sleeper.call_count, 1)
        self.assertEqual(backend.writes[-1], base)
        self.assertEqual(backend.writes.count(base), 1)
        self.assertIsNone(app.effect_runtime)
        self.assertFalse(app.state.effect_running)

    def test_persistent_cleanup_failure_backs_off_until_explicit_interrupt(self):
        class PersistentCleanupFailureBackend(FakeBackend):
            def __init__(self):
                super().__init__()
                self.fail_status = False
                self.status_attempts = 0

            def status(self):
                self.status_attempts += 1
                if self.fail_status:
                    raise BackendIOError("persistent cleanup status failure")
                return super().status()

        backend = PersistentCleanupFailureBackend()
        app = CursesTui(backend, profile_store=FakeProfileStore())
        app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        app._start_effect(now=0.0)
        app._tick_effect(now=0.0, authorized=True)
        writes_before_cleanup = list(backend.writes)
        backend.fail_status = True

        class FailingRenderScreen:
            def keypad(self, _enabled):
                return None

            def timeout(self, _milliseconds):
                return None

            def erase(self):
                raise curses.error("render failed")

        with mock.patch("okeylitctl.tui.initialize_colors", return_value=False), mock.patch(
            "okeylitctl.tui.sleep", side_effect=(None, None, KeyboardInterrupt)
        ) as sleeper:
            app._main(FailingRenderScreen())

        self.assertEqual(sleeper.call_count, 3)
        self.assertEqual(backend.writes, writes_before_cleanup)
        self.assertIsNotNone(app.effect_runtime)
        self.assertTrue(app.state.effect_running)
        self.assertIn("forced", app.state.message.lower())

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

    def test_failed_workspace_render_disables_apply(self):
        backend = FakeBackend()
        app = CursesTui(backend, profile_store=FakeProfileStore())
        app.state.set_selected_color("A1B2C3")

        class HiddenWorkspaceScreen:
            def __init__(self):
                self.keys = iter((ord("a"),))

            def keypad(self, _enabled):
                return None

            def getmaxyx(self):
                return (30, 110)

            def erase(self):
                return None

            def addnstr(self, *_args):
                raise curses.error("workspace hidden")

            def refresh(self):
                raise curses.error("workspace hidden")

            def getch(self):
                try:
                    return next(self.keys)
                except StopIteration as exc:
                    raise curses.error("stop") from exc

        with mock.patch("okeylitctl.tui.initialize_colors", return_value=False):
            app._main(HiddenWorkspaceScreen())

        self.assertEqual(backend.writes, [])
        self.assertTrue(app.state.dirty)

    def test_unannounced_resize_cancels_action_from_stale_workspace(self):
        backend = FakeBackend()
        app = CursesTui(backend, profile_store=FakeProfileStore())
        app.state.set_selected_color("A1B2C3")

        class ResizeDuringInputScreen:
            def __init__(self):
                self.width = 110
                self.keys = iter((ord("a"), ord("q"), ord("y")))

            def keypad(self, _enabled):
                return None

            def getmaxyx(self):
                return (30, self.width)

            def erase(self):
                return None

            def addnstr(self, *_args):
                return None

            def refresh(self):
                return None

            def getch(self):
                key = next(self.keys)
                if key == ord("a"):
                    self.width = 78
                return key

        with mock.patch("okeylitctl.tui.initialize_colors", return_value=False):
            app._main(ResizeDuringInputScreen())

        self.assertEqual(backend.writes, [])

    def test_resize_redraw_does_not_show_internal_safety_prose(self):
        app = CursesTui(FakeBackend(), profile_store=FakeProfileStore())

        class ResizeScreen:
            def __init__(self):
                self.width = 110
                self.refreshes = 0
                self.text = []

            def keypad(self, _enabled):
                pass

            def getmaxyx(self):
                return (30, self.width)

            def erase(self):
                self.text = []

            def addnstr(self, _y, _x, text, _limit, _attr):
                self.text.append(text)

            def refresh(self):
                self.refreshes += 1
                if self.refreshes == 1:
                    self.width = 100

            def getch(self):
                return ord("q")

        screen = ResizeScreen()
        with mock.patch("okeylitctl.tui.initialize_colors", return_value=False):
            app._main(screen)
        self.assertGreaterEqual(screen.refreshes, 2)
        self.assertEqual(app.state.message, "Ready")
        self.assertNotIn("Terminal resized", " ".join(screen.text))

    def test_resize_during_refresh_forces_redraw_before_apply(self):
        class RefreshAwareBackend(FakeBackend):
            def __init__(self):
                super().__init__()
                self.screen = None
                self.refreshes_at_write = 0

            def write_colors(self, value):
                self.refreshes_at_write = self.screen.refreshes
                super().write_colors(value)

        backend = RefreshAwareBackend()
        app = CursesTui(backend, profile_store=FakeProfileStore())
        app.state.set_selected_color("A1B2C3")

        class ResizeDuringRefreshScreen:
            def __init__(self):
                self.width = 110
                self.refreshes = 0
                self.keys = iter((ord("a"), ord("q")))

            def keypad(self, _enabled):
                return None

            def getmaxyx(self):
                return (30, self.width)

            def erase(self):
                return None

            def addnstr(self, *_args):
                return None

            def refresh(self):
                self.refreshes += 1
                if self.refreshes == 1:
                    self.width = 78

            def getch(self):
                return next(self.keys)

        screen = ResizeDuringRefreshScreen()
        backend.screen = screen
        with mock.patch("okeylitctl.tui.initialize_colors", return_value=False):
            app._main(screen)

        self.assertEqual(backend.writes, ["A1B2C3,222222,333333,444444"])
        self.assertGreaterEqual(backend.refreshes_at_write, 2)

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

    def test_profile_equal_to_current_does_not_claim_it_needs_apply(self):
        backend = FakeBackend()
        profiles = FakeProfileStore()
        app = CursesTui(backend, profile_store=profiles)
        profiles.layouts["Current"] = app.state.current

        app._load_profile("Current")

        self.assertFalse(app.state.dirty)
        self.assertNotIn("press A", app.state.message)
        self.assertIn("already active", app.state.message)

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
        self.assertEqual(
            profiles.calls,
            [("list",), ("load", "Work"), ("load", "Work")],
        )
        self.assertEqual(backend.writes, [])

    def test_profile_manager_enter_reloads_selected_profile_at_action_time(self):
        backend = FakeBackend()

        class ChangingStore(FakeProfileStore):
            def __init__(self):
                super().__init__()
                self.loads = 0

            def load(self, name):
                self.calls.append(("load", name))
                self.loads += 1
                if self.loads == 1:
                    return ColorLayout.from_wire("AAAAAA,BBBBBB,CCCCCC,DDDDDD")
                return ColorLayout.from_wire("111111,222222,333333,444444")

        profiles = ChangingStore()
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

        self.assertEqual(app.state.draft.to_wire(), "111111,222222,333333,444444")
        self.assertEqual(
            profiles.calls,
            [("list",), ("load", "Work"), ("load", "Work")],
        )
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

    def test_profile_manager_renames_selected_profile_without_device_access(self):
        backend = FakeBackend()
        profiles = FakeProfileStore()
        app = CursesTui(backend, profile_store=profiles)

        class MenuScreen:
            def __init__(self):
                self.keys = iter((ord("n"), 27))

            def getmaxyx(self):
                return (30, 100)

            def addnstr(self, *_args):
                return None

            def refresh(self):
                return None

            def getch(self):
                return next(self.keys)

            def getstr(self, *_args):
                return b"Office"

        with mock.patch("okeylitctl.tui.curses.echo"), mock.patch(
            "okeylitctl.tui.curses.noecho"
        ):
            app._profile_manager(MenuScreen())

        self.assertIn(("rename", "Work", "Office"), profiles.calls)
        self.assertEqual(profiles.list_names(), ("Office",))
        self.assertEqual(backend.writes, [])

    def test_reference_profile_manager_routes_the_exact_compositor_with_loaded_preview(self):
        backend = FakeBackend()
        profiles = FakeProfileStore()
        app = CursesTui(backend, profile_store=profiles)

        class BufferScreen:
            def __init__(self):
                self.width = 160
                self.height = 43
                self.cells = [[" " for _ in range(self.width)] for _ in range(self.height)]

            def getmaxyx(self):
                return self.height, self.width

            def erase(self):
                self.cells = [[" " for _ in range(self.width)] for _ in range(self.height)]

            def addnstr(self, y, x, text, limit, _attr):
                for offset, character in enumerate(text[:limit]):
                    self.cells[y][x + offset] = character

            def insstr(self, y, x, text, _attr):
                self.cells[y][x] = text

            def refresh(self):
                return None

            def getch(self):
                return 27

        screen = BufferScreen()
        app._profile_manager(screen)
        rendered = "\n".join("".join(row) for row in screen.cells)

        self.assertIn("◆ okeylitctl v0.2.0  ›  Profiles", rendered)
        self.assertIn("PREVIEW · WORK", rendered)
        self.assertIn("#ABCDEF", rendered)
        self.assertIn("load into draft", rendered)
        self.assertIn("rename", rendered)
        self.assertEqual(profiles.calls, [("list",), ("load", "Work")])
        self.assertEqual(backend.writes, [])

    def test_compact_profile_manager_keeps_preview_errors_visible(self):
        backend = FakeBackend()

        class FailingPreviewStore(FakeProfileStore):
            def load(self, name):
                self.calls.append(("load", name))
                raise ProfileError("profile store is malformed")

        profiles = FailingPreviewStore()
        app = CursesTui(backend, profile_store=profiles)

        class RecordingScreen:
            def __init__(self):
                self.text = []

            def getmaxyx(self):
                return (30, 100)

            def addnstr(self, _y, _x, text, _limit, _attr):
                self.text.append(text)

            def refresh(self):
                return None

            def getch(self):
                return 27

        screen = RecordingScreen()
        app._profile_manager(screen)

        self.assertIn("profile store is malformed", " ".join(screen.text))
        self.assertEqual(profiles.calls, [("list",), ("load", "Work")])
        self.assertEqual(backend.writes, [])

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

    def test_profile_delete_fails_closed_if_terminal_resizes_while_yes_is_read(self):
        profiles = FakeProfileStore()
        app = CursesTui(FakeBackend(), profile_store=profiles)

        class ResizeDuringYesScreen:
            def __init__(self):
                self.width = 100
                self.keys = iter((ord("d"), ord("y")))

            def getmaxyx(self):
                return (30, self.width)

            def addnstr(self, *_args):
                return None

            def refresh(self):
                return None

            def getch(self):
                key = next(self.keys)
                if key == ord("y"):
                    self.width = 20
                return key

        app._profile_manager(ResizeDuringYesScreen())

        self.assertNotIn(("delete", "Work"), profiles.calls)
        self.assertIn("rendering error", app.state.message)

    def test_profile_delete_fails_closed_if_resize_occurs_during_refresh(self):
        profiles = FakeProfileStore()
        app = CursesTui(FakeBackend(), profile_store=profiles)

        class ResizeDuringRefreshScreen:
            def __init__(self):
                self.width = 100
                self.refreshes = 0
                self.keys = iter((ord("d"), ord("y")))

            def getmaxyx(self):
                return (30, self.width)

            def addnstr(self, *_args):
                return None

            def refresh(self):
                self.refreshes += 1
                if self.refreshes == 2:
                    self.width = 20

            def getch(self):
                return next(self.keys)

        app._profile_manager(ResizeDuringRefreshScreen())

        self.assertNotIn(("delete", "Work"), profiles.calls)
        self.assertIn("rendering error", app.state.message)

    def test_help_matches_current_effect_bindings(self):
        app = CursesTui(FakeBackend())

        class RecordingScreen:
            def __init__(self):
                self.text = []

            def getmaxyx(self):
                return (30, 100)

            def addnstr(self, _y, _x, text, _limit, _attr):
                self.text.append(text)

        screen = RecordingScreen()
        app._draw_help(screen)
        rendered = " ".join(screen.text)

        self.assertIn("Tab", rendered)
        self.assertIn("effect speed", rendered)
        self.assertIn("effect light", rendered)
        self.assertIn("direction", rendered)
        self.assertIn("Stop", rendered)
        self.assertNotIn("adjust by sixteen", rendered)

    def test_workspace_draws_the_photo_matched_keyboard(self):
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

        for legend in ("Esc", "F12", "Pwr", "Bksp", "NUM", "ENTER"):
            self.assertIn(legend, rendered)
        self.assertIn("┌", rendered)
        self.assertIn("└", rendered)
        self.assertIn("LIVE ▸ DRAFT", rendered)
        self.assertIn("ZONES", rendered)
        for rejected_label in ("PHOTO-MATCHED", "APPROX.", "NOT PER-KEY", "SAFE KEYBOARD LIGHTING"):
            self.assertNotIn(rejected_label, rendered)
        self.assertNotIn("REGION", rendered)

    def test_compact_power_off_effect_state_does_not_claim_stale_live_frame(self):
        app = CursesTui(FakeBackend())
        app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        app.state.effect_running = True
        app.state.effect_frame = ColorLayout.from_wire(
            "AA0000,00BB00,0000CC,DDDD00"
        )
        app.power_state = "off"

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
        self.assertTrue(app._draw(screen))
        rendered = " ".join(screen.text)

        self.assertIn("RESTORE PENDING", rendered)
        self.assertIn("LIVE  POWER OFF", rendered)
        self.assertNotIn("LIVE #AA0000", rendered)
        self.assertNotIn("EFFECT FRAME", rendered)

    def test_compact_workspace_exposes_effect_selection_and_controls(self):
        app = CursesTui(FakeBackend())
        app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        app.state.effect_running = True
        app.state.compact_panel = "effects"

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
        self.assertTrue(app._draw(screen))
        rendered = " ".join(screen.text)

        self.assertIn("EFFECT", rendered)
        self.assertIn("CYCLE", rendered)
        self.assertIn("6/16", rendered)
        self.assertIn("S Stop", rendered)
        self.assertIn("SPEED 60%", rendered)
        self.assertIn("LIGHT 80%", rendered)
        self.assertIn("EDITOR LOCKED", rendered)
        self.assertNotIn("[ ] speed", rendered)

    def test_reference_size_draws_the_approved_editor_composition(self):
        app = CursesTui(FakeBackend())

        class BufferScreen:
            def __init__(self):
                self.width = 160
                self.height = 55
                self.cells = [[" " for _ in range(self.width)] for _ in range(self.height)]
                self.bottom_right_via_insstr = False

            def getmaxyx(self):
                return self.height, self.width

            def erase(self):
                self.cells = [[" " for _ in range(self.width)] for _ in range(self.height)]

            def addnstr(self, y, x, text, limit, _attr):
                for offset, character in enumerate(text[:limit]):
                    self.cells[y][x + offset] = character

            def insch(self, *_args):
                raise OverflowError("byte doesn't fit in chtype")

            def insstr(self, y, x, text, _attr):
                self.bottom_right_via_insstr = True
                self.cells[y][x] = text

            def refresh(self):
                return None

        screen = BufferScreen()
        self.assertTrue(app._draw(screen))
        rendered = "\n".join("".join(row) for row in screen.cells)

        for text in (
            "◆ okeylitctl v0.2.0",
            "IN SYNC",
            "DEVICE ON",
            "KEYBOARD",
            "ZONES",
            "LIVE ▸ DRAFT",
            "EFFECT",
            "COLOR · RIGHT",
            "MOTION",
            "STATIC",
            "RIPPLE",
            "A  apply",
            "Q  quit",
        ):
            with self.subTest(text=text):
                self.assertIn(text, rendered)
        for forbidden in ("PHOTO-MATCHED", "APPROX.", "NOT PER-KEY", "SAFE KEYBOARD LIGHTING"):
            self.assertNotIn(forbidden, rendered)

        self.assertEqual(screen.cells[0][0], "╭")
        self.assertEqual(screen.cells[54][159], "╯")
        self.assertTrue(screen.bottom_right_via_insstr)

    def test_minimum_screen_keeps_every_primary_action_and_zone_visible(self):
        backend = FakeBackend()
        app = CursesTui(backend)

        class BufferScreen:
            def __init__(self, width, height=24):
                self.width = width
                self.height = height
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
                self.assertTrue(app._draw(screen))
                rendered = "\n".join(screen.row(y) for y in range(screen.height))

                for action in (
                    "A apply",
                    "M profiles",
                    "O original",
                    "R refresh",
                    "? help",
                    "Q quit",
                ):
                    self.assertIn(action, rendered)
                for color in ("#111111", "#222222", "#333333", "#444444"):
                    self.assertIn(color, rendered)
                self.assertIn("ZONES", rendered)
                for rejected_label in (
                    "PHOTO-MATCHED",
                    "APPROX.",
                    "NOT PER-KEY",
                    "SAFE KEYBOARD LIGHTING",
                ):
                    self.assertNotIn(rejected_label, rendered)
                if width == 100:
                    for function_key in ("F1", "F10", "F11", "F12"):
                        self.assertIn(function_key, rendered)
                if width <= 79:
                    self.assertIn("ZONES · LIVE ▸ DRAFT", rendered)
                    self.assertIn("COLOR · RIGHT", rendered)
                    self.assertIn("EFFECT STATIC 1/16", rendered)
                    self.assertEqual(screen.row(11)[2], "└")

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

    def test_restore_power_off_noop_preserves_dirty_draft_without_write(self):
        for initially_off in (True, False):
            for same_as_original in (True, False):
                with self.subTest(initially_off=initially_off,
                                  same_as_original=same_as_original):
                    self._assert_restore_power_off_preserves_draft(
                        initially_off=initially_off,
                        same_as_original=same_as_original,
                    )

    def _assert_restore_power_off_preserves_draft(self, *, initially_off, same_as_original):
        backend = FakeBackend(same_as_original=same_as_original)
        if initially_off:
            backend.status_value["state"] = "off"
        app = CursesTui(backend)
        app.state.set_selected_color("ABCDEF")
        draft, original, current = app.state.draft, app.state.original, app.state.current
        backend.status_value["state"] = "off"

        app._restore()

        self.assertEqual(backend.restores, 0)
        self.assertEqual(backend.writes, [])
        self.assertEqual((app.state.draft, app.state.original, app.state.current),
                         (draft, original, current))
        self.assertTrue(app.state.dirty)
        self.assertEqual(app.power_state, "off")
        self.assertIn("power off", app.state.message.lower())
        for composed in (
            compose_editor(app.state, power_state=app.power_state),
            compose_adaptive_editor(app.state, width=110, height=30,
                                    power_state=app.power_state),
            compose_narrow_editor(app.state, width=78, height=24,
                                  power_state=app.power_state),
        ):
            visible = "\n".join(composed.lines)
            self.assertIn("DEVICE OFF", visible)
            self.assertNotIn("IN SYNC", visible)
            self.assertNotIn("LIVE  #111111", visible)
            self.assertFalse(any("swatch_live_" in role or role.startswith("key_right_")
                                 for row in composed.roles for role in row))

    def test_restore_discards_only_local_draft_when_original_is_active(self):
        backend = FakeBackend(same_as_original=True)
        app = CursesTui(backend)
        app.state.set_selected_color("ABCDEF")

        app._restore()

        self.assertEqual(backend.restores, 0)
        self.assertFalse(app.state.dirty)
        self.assertEqual(app.state.draft, app.state.original)
        self.assertIn("local draft", app.state.message)

    def test_restore_rechecks_live_device_before_skipping_firmware_restore(self):
        backend = FakeBackend(same_as_original=True)
        app = CursesTui(backend)
        backend.status_value["colors"] = [
            "999999",
            "888888",
            "777777",
            "666666",
        ]

        app._restore()

        self.assertEqual(backend.restores, 1)

    def test_restore_uses_new_module_original_after_external_reload(self):
        backend = FakeBackend(same_as_original=True)
        app = CursesTui(backend)
        new_original = ["555555", "666666", "777777", "888888"]
        backend.status_value["original"] = new_original

        app._restore()

        expected = ColorLayout.from_wire(",".join(new_original))
        self.assertEqual(backend.restores, 1)
        self.assertEqual(app.state.original, expected)
        self.assertEqual(app.state.current, expected)
        self.assertEqual(app.state.draft, expected)

    def test_restore_noop_adopts_new_live_original_after_external_reload(self):
        backend = FakeBackend(same_as_original=True)
        app = CursesTui(backend)
        new_original = ["555555", "666666", "777777", "888888"]
        backend.status_value["colors"] = new_original
        backend.status_value["original"] = new_original

        app._restore()

        expected = ColorLayout.from_wire(",".join(new_original))
        self.assertEqual(backend.restores, 0)
        self.assertEqual(app.state.original, expected)
        self.assertEqual(app.state.current, expected)
        self.assertEqual(app.state.draft, expected)

    def test_restore_records_original_verified_after_restore_write(self):
        class ReloadingBackend(FakeBackend):
            def restore(self):
                self.restores += 1
                new_original = ["555555", "666666", "777777", "888888"]
                self.status_value["original"] = new_original
                self.status_value["colors"] = new_original

        backend = ReloadingBackend()
        app = CursesTui(backend)

        app._restore()

        expected = ColorLayout.from_wire("555555,666666,777777,888888")
        self.assertEqual(backend.restores, 1)
        self.assertEqual(app.state.original, expected)
        self.assertEqual(app.state.current, expected)
        self.assertEqual(app.state.draft, expected)

    def test_apply_writes_one_complete_layout_then_marks_it_current(self):
        backend = FakeBackend()
        app = CursesTui(backend)
        app.state.set_selected_color("ABCDEF")

        app._apply()

        self.assertEqual(backend.writes, ["ABCDEF,222222,333333,444444"])
        self.assertEqual(app.state.current, app.state.draft)

    def test_reference_exact_hex_modal_temporarily_uses_blocking_input(self):
        app = CursesTui(FakeBackend())

        class TimedModalScreen:
            def __init__(self):
                self.timeouts = []

            def timeout(self, value):
                self.timeouts.append(value)

            def getmaxyx(self):
                return 56, 162

            def addnstr(self, *_args):
                return None

            def refresh(self):
                return None

            def getch(self):
                return 27

            def move(self, *_args):
                return None

        screen = TimedModalScreen()
        with mock.patch("okeylitctl.tui.curses.curs_set"):
            app._edit_color(screen)

        self.assertEqual(screen.timeouts, [-1, 50])

    def test_reference_exact_hex_modal_validates_then_updates_only_local_draft(self):
        backend = FakeBackend()
        app = CursesTui(backend)
        app.state.selected_zone = Zone.WASD

        class ModalScreen:
            def __init__(self):
                self.keys = iter([*(ord(char) for char in "FF9E3"), 10, ord("A"), 10])
                self.rendered = []

            def getmaxyx(self):
                return 55, 160

            def addnstr(self, _y, _x, text, _limit, _attr):
                self.rendered.append(text)

            def refresh(self):
                return None

            def getch(self):
                return next(self.keys)

            def move(self, *_args):
                return None

        screen = ModalScreen()
        with mock.patch("okeylitctl.tui.curses.curs_set"):
            app._edit_color(screen)

        self.assertEqual(app.state.draft.wasd, "FF9E3A")
        self.assertEqual(app.state.current.wasd, "444444")
        self.assertEqual(backend.writes, [])
        rendered = " ".join(screen.rendered)
        self.assertIn("needs 1 more digit", rendered)
        self.assertIn("EXACT HEX · WASD", rendered)

    def test_reference_exact_hex_modal_escape_preserves_draft(self):
        app = CursesTui(FakeBackend())
        original = app.state.draft

        class CancelScreen:
            def getmaxyx(self):
                return 56, 162

            def addnstr(self, *_args):
                return None

            def refresh(self):
                return None

            def getch(self):
                return 27

            def move(self, *_args):
                return None

        app._edit_color(CancelScreen())

        self.assertEqual(app.state.draft, original)
        self.assertIn("cancelled", app.state.message.lower())

    def test_reference_exact_hex_modal_resize_cancels_pending_input(self):
        app = CursesTui(FakeBackend())
        original = app.state.draft

        class ResizingModalScreen:
            def __init__(self):
                self.width = 162

            def getmaxyx(self):
                return 56, self.width

            def addnstr(self, *_args):
                return None

            def refresh(self):
                return None

            def getch(self):
                self.width = 100
                return ord("A")

            def move(self, *_args):
                return None

        app._edit_color(ResizingModalScreen())

        self.assertEqual(app.state.draft, original)
        self.assertIn("cancelled", app.state.message.lower())

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

            def getch(self):
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

    def test_profile_name_prompt_temporarily_uses_blocking_input(self):
        app = CursesTui(FakeBackend(), profile_store=FakeProfileStore())

        class TimedNameScreen:
            def __init__(self):
                self.timeout_value = 50
                self.timeouts = []

            def timeout(self, value):
                self.timeout_value = value
                self.timeouts.append(value)

            def getmaxyx(self):
                return (30, 100)

            def addnstr(self, *_args):
                return None

            def refresh(self):
                return None

            def getstr(self, *_args):
                return b"Work" if self.timeout_value == -1 else b""

        screen = TimedNameScreen()
        with mock.patch("okeylitctl.tui.curses.echo"), mock.patch(
            "okeylitctl.tui.curses.noecho"
        ), mock.patch("okeylitctl.tui.curses.curs_set"):
            name = app._prompt_profile_name(screen)

        self.assertEqual(name, "Work")
        self.assertEqual(screen.timeouts, [-1, 50])

    def test_profile_name_prompt_rejects_resize_during_refresh(self):
        app = CursesTui(FakeBackend(), profile_store=FakeProfileStore())

        class ResizeDuringRefreshScreen:
            def __init__(self):
                self.width = 100
                self.reads = 0

            def getmaxyx(self):
                return (30, self.width)

            def addnstr(self, *_args):
                return None

            def refresh(self):
                self.width = 20

            def getstr(self, *_args):
                self.reads += 1
                return b"HiddenSave"

        screen = ResizeDuringRefreshScreen()
        with mock.patch("okeylitctl.tui.curses.echo"), mock.patch(
            "okeylitctl.tui.curses.noecho"
        ), mock.patch("okeylitctl.tui.curses.curs_set"):
            name = app._prompt_profile_name(screen)

        self.assertIsNone(name)
        self.assertEqual(screen.reads, 0)
        self.assertIn("cancelled", app.state.message.lower())

    def test_compact_color_prompt_temporarily_uses_blocking_input(self):
        app = CursesTui(FakeBackend())

        class TimedColorScreen:
            def __init__(self):
                self.timeout_value = 50
                self.timeouts = []

            def timeout(self, value):
                self.timeout_value = value
                self.timeouts.append(value)

            def getmaxyx(self):
                return (30, 100)

            def addnstr(self, *_args):
                return None

            def refresh(self):
                return None

            def getch(self):
                if self.timeout_value != -1:
                    raise AssertionError("modal input must block while waiting")
                return self.keys.pop(0)

            def getstr(self, *_args):
                raise AssertionError("compact color entry must use the guided modal")

        screen = TimedColorScreen()
        screen.keys = [*(ord(character) for character in "ABCDEF"), 10]
        with mock.patch("okeylitctl.tui.curses.echo"), mock.patch(
            "okeylitctl.tui.curses.noecho"
        ), mock.patch("okeylitctl.tui.curses.curs_set"):
            app._edit_color(screen)

        self.assertEqual(app.state.draft.right, "ABCDEF")
        self.assertEqual(screen.timeouts, [-1, 50])

    def test_color_prompt_rejects_resize_during_refresh(self):
        app = CursesTui(FakeBackend())
        original = app.state.draft

        class ResizeDuringRefreshScreen:
            def __init__(self):
                self.width = 100
                self.reads = 0

            def getmaxyx(self):
                return (30, self.width)

            def addnstr(self, *_args):
                return None

            def refresh(self):
                self.width = 20

            def getstr(self, *_args):
                self.reads += 1
                return b"ABCDEF"

        screen = ResizeDuringRefreshScreen()
        with mock.patch("okeylitctl.tui.curses.echo"), mock.patch(
            "okeylitctl.tui.curses.noecho"
        ), mock.patch("okeylitctl.tui.curses.curs_set"):
            app._edit_color(screen)

        self.assertEqual(screen.reads, 0)
        self.assertEqual(app.state.draft, original)
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
