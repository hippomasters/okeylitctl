"""Device-free responsive rendering tests at ordinary terminal font sizes."""

import unittest
from unittest import mock

from okeylitctl.tui import CursesTui, TuiCommand, handle_key
from okeylitctl import tui_compositor
from okeylitctl.models import ColorLayout
from okeylitctl.effects import EffectKind, frame_at


class ReadOnlyBackend:
    def status(self):
        return {
            "state": "on",
            "colors": ["710FFA", "710FFA", "710FFA", "0FFA36"],
            "original": ["710FFA", "710FFA", "710FFA", "0FFA36"],
        }


class StrictScreen:
    def __init__(self, width, height):
        self.width, self.height = width, height
        self.erase()

    def getmaxyx(self):
        return self.height, self.width

    def erase(self):
        self.cells = [[" " for _ in range(self.width)] for _ in range(self.height)]

    def addnstr(self, y, x, text, limit, _attr=0):
        written = text[:limit]
        if y < 0 or y >= self.height or x < 0 or x + len(written) > self.width:
            raise AssertionError("renderer wrote outside terminal")
        for offset, character in enumerate(written):
            self.cells[y][x + offset] = character

    def insstr(self, y, x, text, attr=0):
        self.addnstr(y, x, text, len(text), attr)

    def refresh(self):
        pass

    def lines(self):
        return ["".join(row) for row in self.cells]


class ResponsiveEditorTests(unittest.TestCase):
    def test_prestart_cycle_preview_is_local_and_live_remains_verified(self):
        backend = ReadOnlyBackend()
        app = CursesTui(backend)
        app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        current, draft = app.state.current, app.state.draft

        app._update_preview(now=0.0)
        first = frame_at(app.state.effect_spec, 0.0)
        self.assertEqual(app.state.preview_frame, first)
        composed = tui_compositor.compose_editor(app.state, power_state="on")
        visible = "\n".join(composed.lines)
        self.assertIn("KEYBOARD · PREVIEW", visible)
        self.assertIn("LIVE", visible)
        self.assertEqual(app.state.current, current)
        self.assertEqual(app.state.draft, draft)
        self.assertTrue(any(
            role.startswith(f"key_right_{first.right}")
            for row in composed.roles for role in row
        ))

        app._update_preview(now=1.0)
        self.assertNotEqual(app.state.preview_frame, first)
        self.assertEqual(app.state.current, current)
        self.assertEqual(app.state.draft, draft)

    def test_reactive_and_ripple_preview_pulse_without_device_events(self):
        app = CursesTui(ReadOnlyBackend())
        for kind in (EffectKind.REACTIVE, EffectKind.RIPPLE):
            with self.subTest(kind=kind):
                app.state.effect_index = tuple(EffectKind).index(kind)
                app._update_preview(now=0.0)
                first = app.state.preview_frame
                self.assertIsNotNone(first)
                self.assertNotEqual(first, app.state.draft)
                app._update_preview(now=0.3)
                self.assertNotEqual(app.state.preview_frame, first)
                self.assertEqual(app.state.current, app.state.draft)

    def test_cycle_input_changes_preview_and_the_spec_used_when_started(self):
        app = CursesTui(ReadOnlyBackend())
        app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        app._update_preview(now=0.9)
        before = app.state.preview_frame

        handle_key(app.state, ord("+"))
        app._update_preview(now=0.9)
        after = app.state.preview_frame
        self.assertNotEqual(after, before)
        self.assertTrue(app._start_effect(now=0.0))
        self.assertEqual(app.effect_runtime.spec.cycle_from_deg, 5)
        self.assertEqual(frame_at(app.effect_runtime.spec, 0.9), after)
        app._update_preview(now=1.0)
        self.assertIsNone(app.state.preview_frame)
        composed = tui_compositor.compose_editor(app.state, power_state="on")
        self.assertNotIn("KEYBOARD · PREVIEW", "\n".join(composed.lines))
        self.assertIn("key_unavailable", {
            role for row in composed.roles for role in row
        })

    def test_short_terminal_switches_color_and_effects_without_device_access(self):
        app = CursesTui(ReadOnlyBackend())
        screen = StrictScreen(78, 24)
        self.assertTrue(app._draw(screen))
        color_view = "\n".join(screen.lines())
        self.assertIn("COLOR · RIGHT", color_view)
        self.assertIn("RED", color_view)
        self.assertIn("EFFECT", color_view)
        self.assertIn("V", color_view)

        self.assertEqual(handle_key(app.state, ord("v")), TuiCommand.NONE)
        self.assertTrue(app._draw(screen))
        effects_view = "\n".join(screen.lines())
        self.assertIn("EFFECT · STATIC", effects_view)
        self.assertIn("COLOR", effects_view)
        self.assertNotEqual(color_view, effects_view)
        self.assertEqual(handle_key(app.state, ord("v")), TuiCommand.NONE)
        self.assertTrue(app._draw(screen))
        self.assertEqual(color_view, "\n".join(screen.lines()))

    def test_help_explains_the_compact_panel_switch_at_minimum_size(self):
        app = CursesTui(ReadOnlyBackend())
        app.state.help_visible = True
        screen = StrictScreen(78, 24)
        self.assertTrue(app._draw(screen))
        visible = "\n".join(screen.lines())
        self.assertIn("V", visible)
        self.assertIn("Color / Effects panel", visible)
        self.assertIn("Press ? or Esc to close", visible)

    def test_help_explains_cycle_controls_and_deliberate_start(self):
        app = CursesTui(ReadOnlyBackend())
        app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        app.state.help_visible = True
        for width, height in ((78, 24), (160, 55)):
            with self.subTest(size=(width, height)):
                screen = StrictScreen(width, height)
                self.assertTrue(app._draw(screen))
                visible = "\n".join(screen.lines())
                for label in ("Cycle FROM/TO/SAT", "base RGB", "Cycle palettes",
                              "start an effect"):
                    self.assertIn(label, visible)

    def test_normal_font_110_by_30_focuses_colored_sliders_or_effects(self):
        app = CursesTui(ReadOnlyBackend())
        screen = StrictScreen(110, 30)
        self.assertTrue(app._draw(screen))
        visible = "\n".join(screen.lines())
        for label in ("KEYBOARD", "ZONES", "LIVE ▸ DRAFT", "COLOR · RIGHT",
                      "RED", "GREEN", "BLUE", "V EFFECTS"):
            self.assertIn(label, visible)
        self.assertNotIn("SCANNER", visible)

        handle_key(app.state, ord("v"))
        self.assertTrue(app._draw(screen))
        visible = "\n".join(screen.lines())
        for label in ("EFFECT · STATIC", "SCANNER", "SPEED", "LIGHT", "V COLOR"):
            self.assertIn(label, visible)

    def test_short_cycle_color_panel_shows_actual_rainbow_and_spectrum_controls(self):
        app = CursesTui(ReadOnlyBackend())
        app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        for width, height in ((78, 24), (78, 38)):
            with self.subTest(size=(width, height)):
                composed = tui_compositor.compose_narrow_editor(
                    app.state, width=width, height=height, power_state="on"
                )
                visible = "\n".join(composed.lines)
                for label in ("COLOR · SPECTRUM", "FROM", "TO", "SAT"):
                    self.assertIn(label, visible)
                self.assertGreaterEqual(len({
                    role for row in composed.roles for role in row
                    if role.startswith("spectrum_")
                }), 6)

    def test_cycle_footer_explains_spectrum_input_instead_of_rgb_input(self):
        app = CursesTui(ReadOnlyBackend())
        app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        for width, height in ((78, 24), (110, 30), (159, 54), (160, 55)):
            with self.subTest(size=(width, height)):
                screen = StrictScreen(width, height)
                self.assertTrue(app._draw(screen))
                footer = "\n".join(screen.lines()[-6:])
                self.assertIn("FROM/TO/SAT", footer)
                self.assertNotIn("↑↓ RGB", footer)

    def test_tall_narrow_cycle_effect_strip_is_multicolor(self):
        app = CursesTui(ReadOnlyBackend())
        app.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        composed = tui_compositor.compose_narrow_editor(
            app.state, width=78, height=55, power_state="on"
        )
        hues = {
            role for row in composed.roles for role in row
            if role.startswith("spectrum_")
        }
        self.assertGreaterEqual(len(hues), 6)
        self.assertNotIn("effect_preview_cycle", {
            role for row in composed.roles for role in row
        })

    def test_larger_than_reference_terminals_use_the_full_viewport(self):
        app = CursesTui(ReadOnlyBackend())
        for width, height in ((160, 56), (161, 55), (268, 59)):
            with self.subTest(size=(width, height)):
                screen = StrictScreen(width, height)
                self.assertTrue(app._draw(screen))
                lines = screen.lines()
                self.assertEqual(lines[0][0], "╭")
                self.assertEqual(lines[0][-1], "╮")
                self.assertEqual(lines[-1][0], "╰")
                self.assertEqual(lines[-1][-1], "╯")

    def test_help_intercepts_reactive_input_before_effect_event(self):
        app = CursesTui(ReadOnlyBackend())
        app.state.effect_running = True

        class InputScreen(StrictScreen):
            def __init__(self):
                super().__init__(110, 30)
                self.keys = iter((ord("?"), ord("a"), ord("?"), ord("q")))

            def keypad(self, _enabled):
                pass

            def timeout(self, _milliseconds):
                pass

            def getch(self):
                return next(self.keys)

        with mock.patch("okeylitctl.tui.initialize_colors", return_value=False), mock.patch.object(
            app, "_tick_effect", return_value=True
        ), mock.patch.object(
            app, "_trigger_effect_input", side_effect=AssertionError("Help key triggered effect")
        ) as trigger:
            app._main(InputScreen())
        trigger.assert_not_called()
        self.assertFalse(app.state.help_visible)

    def test_running_reactive_effect_allows_view_switch_without_an_event(self):
        app = CursesTui(ReadOnlyBackend())
        app.state.effect_index = tuple(EffectKind).index(EffectKind.REACTIVE)
        app.state.effect_running = True

        class InputScreen(StrictScreen):
            def __init__(self):
                super().__init__(110, 30)
                self.keys = iter((ord("v"), ord("q")))

            def keypad(self, _enabled):
                pass

            def timeout(self, _milliseconds):
                pass

            def getch(self):
                return next(self.keys)

        with mock.patch("okeylitctl.tui.initialize_colors", return_value=False), mock.patch.object(
            app, "_tick_effect", return_value=True
        ), mock.patch.object(
            app, "_trigger_effect_input", side_effect=AssertionError("V triggered effect")
        ) as trigger:
            app._main(InputScreen())
        trigger.assert_not_called()
        self.assertEqual(app.state.compact_panel, "effects")

    def test_failed_render_cannot_turn_v_into_hidden_reactive_input(self):
        app = CursesTui(ReadOnlyBackend())
        app.state.effect_index = tuple(EffectKind).index(EffectKind.REACTIVE)
        app.state.effect_running = True

        class InputScreen(StrictScreen):
            def __init__(self):
                super().__init__(110, 30)
                self.keys = iter((ord("v"), ord("q")))

            def keypad(self, _enabled):
                pass

            def timeout(self, _milliseconds):
                pass

            def getch(self):
                return next(self.keys)

        def failed_draw(_screen):
            app._rendered_geometry = (30, 110)
            return False

        with mock.patch("okeylitctl.tui.initialize_colors", return_value=False), mock.patch.object(
            app, "_draw", side_effect=failed_draw
        ), mock.patch.object(
            app, "_tick_effect", return_value=False
        ), mock.patch.object(
            app, "_trigger_effect_input", side_effect=AssertionError("hidden event")
        ) as trigger:
            app._main(InputScreen())
        trigger.assert_not_called()
        self.assertEqual(app.state.compact_panel, "color")

    def test_verified_restore_pending_uses_restore_not_stop_instruction(self):
        app = CursesTui(ReadOnlyBackend())
        app.state.effect_running = True
        app.state.effect_restore_pending = True
        app.state.effect_frame = ColorLayout.from_wire(
            "AA0000,00BB00,0000CC,DDDD00"
        )
        for width, height in ((78, 24), (100, 30), (159, 54)):
            with self.subTest(size=(width, height)):
                screen = StrictScreen(width, height)
                self.assertTrue(app._draw(screen))
                visible = "\n".join(screen.lines())
                self.assertIn("RESTORE PENDING", visible)
                self.assertIn("S Restore", visible)
                self.assertNotIn("S Stop", visible)

    def test_power_off_restore_pending_requires_power_on_before_retry(self):
        app = CursesTui(ReadOnlyBackend())
        app.state.effect_running = True
        app.state.effect_restore_pending = True
        app.state.effect_frame = ColorLayout.from_wire(
            "AA0000,00BB00,0000CC,DDDD00"
        )
        app.power_state = "off"
        for width, height in ((78, 24), (100, 30), (159, 54)):
            with self.subTest(size=(width, height)):
                screen = StrictScreen(width, height)
                self.assertTrue(app._draw(screen))
                visible = "\n".join(screen.lines())
                self.assertIn("RESTORE PENDING", visible)
                self.assertIn("POWER ON, THEN S TO RESTORE", visible)
                self.assertNotIn("S Verify", visible)
                self.assertNotIn("S Restore", visible)
                self.assertNotIn("Q quit", visible)

    def test_reference_footer_only_advertises_available_effect_actions(self):
        app = CursesTui(ReadOnlyBackend())
        app.state.effect_running = True
        app.state.effect_frame = ColorLayout.from_wire(
            "AA0000,00BB00,0000CC,DDDD00"
        )
        for pending, uncertain, power, expected, banner in (
            (False, False, "on", "S Stop", "EFFECT ACTIVE"),
            (True, False, "on", "S Restore", "RESTORE PENDING"),
            (False, True, "on", "S Verify", "EFFECT UNKNOWN"),
            (True, False, "off", "POWER ON, THEN S TO RESTORE", "RESTORE PENDING"),
        ):
            with self.subTest(pending=pending, uncertain=uncertain, power=power):
                app.state.effect_restore_pending = pending
                app.state.effect_frame_uncertain = uncertain
                app.power_state = power
                screen = StrictScreen(160, 55)
                self.assertTrue(app._draw(screen))
                footer = "\n".join(screen.lines()[49:53])
                self.assertIn(banner, screen.lines()[49])
                self.assertIn(expected, footer)
                for unavailable in ("Tab  effect", "X  discard", "O  original", "M  profiles"):
                    self.assertNotIn(unavailable, footer)
                if power == "off":
                    self.assertNotIn("Q  quit", footer)

    def test_responsive_effect_footers_hide_locked_editor_shortcuts(self):
        app = CursesTui(ReadOnlyBackend())
        app.state.effect_running = True
        for width, height in ((78, 24), (100, 30), (159, 54)):
            for pending, uncertain, power in (
                (False, False, "on"),
                (True, False, "on"),
                (False, True, "on"),
                (True, False, "off"),
            ):
                with self.subTest(size=(width, height), pending=pending,
                                  uncertain=uncertain, power=power):
                    app.state.effect_restore_pending = pending
                    app.state.effect_frame_uncertain = uncertain
                    app.power_state = power
                    screen = StrictScreen(width, height)
                    self.assertTrue(app._draw(screen))
                    footer = "\n".join(screen.lines()[height - 3:height - 1])
                    self.assertIn("EDITOR LOCKED", footer)
                    for unavailable in ("zone", "RGB", "adjust", "Tab effect",
                                        "speed", "E hex", "P preset"):
                        self.assertNotIn(unavailable, footer)

    def test_uncertain_restore_pending_reports_unknown_until_verified(self):
        app = CursesTui(ReadOnlyBackend())
        app.state.effect_running = True
        app.state.effect_restore_pending = True
        app.state.effect_frame_uncertain = True
        app.state.effect_frame = ColorLayout.from_wire(
            "AA0000,00BB00,0000CC,DDDD00"
        )
        for width, height in ((78, 24), (100, 30), (159, 54), (160, 55)):
            with self.subTest(size=(width, height)):
                screen = StrictScreen(width, height)
                self.assertTrue(app._draw(screen))
                visible = "\n".join(screen.lines())
                self.assertIn("EFFECT UNKNOWN", screen.lines()[1])
                self.assertNotIn("RESTORE PENDING", screen.lines()[1])
                self.assertIn("UNKNOWN", visible)
                self.assertIn("S Verify", visible)
                self.assertNotIn("#AA0000", visible)

    def test_help_is_visible_and_closable_on_each_editor_layout(self):
        app = CursesTui(ReadOnlyBackend())
        app.state.help_visible = True
        for width, height in ((78, 24), (100, 30), (159, 54), (160, 55)):
            with self.subTest(size=(width, height)):
                screen = StrictScreen(width, height)
                self.assertTrue(app._draw(screen))
                visible = "\n".join(screen.lines())
                self.assertIn("OKEYLITCTL HELP", visible)
                self.assertIn("Press ? or Esc to close", visible)

    def test_action_failure_message_is_visible_on_each_editor_layout(self):
        app = CursesTui(ReadOnlyBackend())
        app.state.message = "Apply failed: readback mismatch"
        for width, height in ((78, 24), (100, 30), (159, 54), (160, 55)):
            with self.subTest(size=(width, height)):
                screen = StrictScreen(width, height)
                self.assertTrue(app._draw(screen))
                self.assertIn(app.state.message, "\n".join(screen.lines()))

    def test_responsive_status_badges_do_not_style_uncertainty_as_synced(self):
        app = CursesTui(ReadOnlyBackend())
        for width, height in ((78, 24), (100, 30), (159, 54)):
            for state_name, pending, uncertain, power, expected, role in (
                ("active", False, False, "on", "EFFECT ACTIVE", "effect_badge"),
                ("uncertain", False, True, "on", "EFFECT UNKNOWN", "error"),
                ("restore", True, False, "on", "RESTORE PENDING", "error"),
                ("power-off", False, False, "off", "RESTORE PENDING", "error"),
            ):
                with self.subTest(size=(width, height), state=state_name):
                    app.state.effect_running = True
                    app.state.effect_restore_pending = pending
                    app.state.effect_frame_uncertain = uncertain
                    composed = (
                        tui_compositor.compose_adaptive_editor(
                            app.state, width=width, height=height, power_state=power
                        ) if width >= 100 and height >= 30 else
                        tui_compositor.compose_narrow_editor(
                            app.state, width=width, height=height, power_state=power
                        )
                    )
                    column = composed.lines[1].index(expected)
                    self.assertEqual(composed.roles[1][column], role)

    def test_narrow_profiles_keep_saved_zone_color_preview_without_firmware(self):
        app = CursesTui(ReadOnlyBackend())
        screen = StrictScreen(78, 24)
        preview = ColorLayout.from_wire("FF5E5A,FF9E3D,FFD23F,FFF1D6")
        self.assertTrue(app._draw_profile_manager(
            screen, ("Sunset",), 0, preview_layout=preview,
        ))
        visible = "\n".join(screen.lines())
        self.assertIn("LOCAL PROFILES", visible)
        self.assertIn("Sunset", visible)
        for color in ("#FF5E5A", "#FF9E3D", "#FFD23F", "#FFF1D6"):
            self.assertIn(color, visible)

    def test_undersized_terminal_cannot_open_hidden_color_prompt(self):
        app = CursesTui(ReadOnlyBackend())

        class TooSmall(StrictScreen):
            def __init__(self):
                super().__init__(77, 24)
                self.reads = 0

            def getstr(self, *_args):
                self.reads += 1
                return b"ABCDEF"

        screen = TooSmall()
        original = app.state.draft
        app._edit_color(screen)
        self.assertEqual(screen.reads, 0)
        self.assertEqual(app.state.draft, original)
        self.assertIn("small", app.state.message.lower())

    def test_profiles_preview_reflows_with_keyboard_at_normal_font(self):
        app = CursesTui(ReadOnlyBackend())
        preview = ColorLayout.from_wire("FF5E5A,FF9E3D,FFD23F,FFF1D6")
        composed = tui_compositor.compose_responsive_profiles(
            app.state, width=110, height=30, names=("Sunset",), selected=0,
            preview_layout=preview, power_state="on", version="0.2.0",
        )
        visible = "\n".join(composed.lines)
        for label in ("PROFILES", "PREVIEW · SUNSET", "Esc", "#FF5E5A",
                      "#FF9E3D", "#FFD23F", "#FFF1D6", "Enter", "delete"):
            self.assertIn(label, visible)
        self.assertTrue(any(role.startswith("profile_key_")
                            for row in composed.roles for role in row))
        self.assertEqual((composed.width, composed.height), (110, 30))

    def test_responsive_profiles_keep_max_name_delete_prompt_and_borders_visible(self):
        app = CursesTui(ReadOnlyBackend())
        name = "A" * 32
        composed = tui_compositor.compose_responsive_profiles(
            app.state, width=100, height=30, names=(name,), selected=0,
            preview_layout=app.state.current, power_state="on", pending_delete=name,
        )
        self.assertIn(name, "\n".join(composed.lines))
        self.assertIn("Y confirm · N cancel", "\n".join(composed.lines))
        self.assertEqual(composed.lines[3][39], "┐")
        self.assertEqual(composed.lines[25][39], "┘")
        self.assertEqual(composed.lines[29][-1], "╯")

    def test_responsive_profile_error_does_not_invent_a_saved_keyboard(self):
        app = CursesTui(ReadOnlyBackend())
        composed = tui_compositor.compose_responsive_profiles(
            app.state, width=100, height=30, names=("Missing",), selected=0,
            preview_layout=None, preview_error="unsafe profile", power_state="on",
        )
        self.assertIn("Preview unavailable", "\n".join(composed.lines))
        self.assertIn("unsafe profile", "\n".join(composed.lines))
        self.assertFalse(any(role.startswith("profile_key_")
                             for row in composed.roles for role in row))

    def test_exact_color_entry_uses_visible_guided_modal_at_110_by_30(self):
        app = CursesTui(ReadOnlyBackend())

        class ModalScreen(StrictScreen):
            def timeout(self, _delay):
                pass

            def getch(self):
                return 27

            def move(self, *_args):
                pass

        screen = ModalScreen(110, 30)
        app._edit_color(screen)
        rendered = "\n".join(screen.lines())
        self.assertIn("EXACT HEX · RIGHT", rendered)
        self.assertIn("waiting for six digits", rendered)
        self.assertIn("Esc", rendered)
        self.assertEqual(app.state.current.to_wire(), app.state.draft.to_wire())

    def test_exact_hex_modal_keeps_full_viewport_above_reference_size(self):
        app = CursesTui(ReadOnlyBackend())

        class ModalScreen(StrictScreen):
            def timeout(self, _delay):
                pass

            def getch(self):
                return 27

            def move(self, *_args):
                pass

        for width, height in ((160, 56), (161, 55), (268, 59)):
            with self.subTest(size=(width, height)):
                screen = ModalScreen(width, height)
                app._edit_color(screen)
                lines = screen.lines()
                self.assertEqual(lines[0][0], "╭")
                self.assertEqual(lines[0][-1], "╮")
                self.assertEqual(lines[-1][0], "╰")
                self.assertEqual(lines[-1][-1], "╯")
                self.assertIn("EXACT HEX · RIGHT", "\n".join(lines))

    def test_guided_exact_hex_modal_at_normal_font_keeps_preview_and_actions(self):
        app = CursesTui(ReadOnlyBackend())
        for width, height in ((78, 24), (110, 30), (159, 54)):
            with self.subTest(size=(width, height)):
                composed = tui_compositor.compose_responsive_hex_modal(
                    app.state, width=width, height=height, power_state="on",
                    input_text="FF9E3", error="needs 1 more digit", version="0.2.0",
                )
                visible = "\n".join(composed.lines)
                for label in ("EXACT HEX · RIGHT", "# FF9E3", "needs 1 more digit",
                              "LIVE", "NEW", "#710FFA", "Enter", "accept", "Esc", "cancel"):
                    self.assertIn(label, visible)
                self.assertEqual((composed.width, composed.height), (width, height))

    def test_ordinary_110_by_30_terminal_keeps_mockup_panels_without_font_zoom(self):
        app = CursesTui(ReadOnlyBackend())
        screen = StrictScreen(110, 30)
        self.assertTrue(app._draw(screen))
        lines = screen.lines()
        visible = "\n".join(lines)
        for label in ("KEYBOARD", "ZONES", "LIVE ▸ DRAFT", "EFFECT", "COLOR · RIGHT",
                      "RED", "GREEN", "BLUE", "V EFFECTS"):
            with self.subTest(label=label):
                self.assertIn(label, visible)
        self.assertIn("STATIC", visible)
        self.assertNotIn("SCANNER", visible)
        handle_key(app.state, ord("v"))
        self.assertTrue(app._draw(screen))
        self.assertIn("SCANNER", "\n".join(screen.lines()))
        self.assertIn("Esc", visible)
        self.assertIn("#710FFA", visible)
        self.assertIn("A apply", visible)
        self.assertEqual(lines[0][0], "╭")
        self.assertEqual(lines[-1][-1], "╯")

    def test_footer_never_overwrites_color_panel_or_outer_border_at_30_rows(self):
        app = CursesTui(ReadOnlyBackend())
        screen = StrictScreen(110, 30)
        self.assertTrue(app._draw(screen))
        lines = screen.lines()
        self.assertEqual(lines[26][2], "└")
        self.assertEqual(lines[26][3:20], "─" * 17)
        self.assertIn("A apply", lines[28])
        self.assertEqual(lines[29][0], "╰")
        self.assertEqual(lines[29][-1], "╯")

    def test_minimum_78_by_24_reflows_without_tiny_fonts_or_hidden_actions(self):
        app = CursesTui(ReadOnlyBackend())
        screen = StrictScreen(78, 24)
        self.assertTrue(app._draw(screen))
        lines = screen.lines()
        visible = "\n".join(lines)
        for label in ("KEYBOARD", "ZONES", "LIVE", "DRAFT", "EFFECT", "STATIC",
                      "COLOR · RIGHT", "RED", "#710FFA", "#0FFA36",
                      "A apply", "M profiles", "Q quit"):
            with self.subTest(label=label):
                self.assertIn(label, visible)
        self.assertEqual(lines[0][0], "╭")
        self.assertEqual(lines[23][77], "╯")
        self.assertEqual(lines[11][2], "└")

    def test_tall_narrow_terminal_uses_extra_rows_for_effects_color_and_motion(self):
        app = CursesTui(ReadOnlyBackend())
        screen = StrictScreen(78, 55)
        self.assertTrue(app._draw(screen))
        lines = screen.lines()
        visible = "\n".join(lines)
        for label in ("KEYBOARD", "ZONES", "EFFECT", "STATIC", "SCANNER",
                      "COLOR · RIGHT", "MOTION", "RED", "GREEN", "BLUE", "Q quit"):
            self.assertIn(label, visible)
        keyboard_bottom = next(i for i, line in enumerate(lines) if line[2] == "└")
        self.assertLessEqual(keyboard_bottom, 24)

    def test_narrow_38_row_boundary_keeps_the_full_effect_catalog(self):
        app = CursesTui(ReadOnlyBackend())
        app.state.compact_panel = "effects"
        screen = StrictScreen(78, 38)
        self.assertTrue(app._draw(screen))
        visible = "\n".join(screen.lines())
        for label in ("STATIC", "BLINK", "CYCLE", "RIPPLE", "SCANNER"):
            self.assertIn(label, visible)

    def test_adjacent_terminal_sizes_keep_a_complete_safe_editor(self):
        app = CursesTui(ReadOnlyBackend())
        for height, width in ((24, 78), (24, 79), (25, 80), (29, 99),
                              (29, 100), (30, 100), (30, 101), (31, 110),
                              (42, 120), (43, 159), (54, 159), (54, 160),
                              (55, 160), (55, 161), (59, 268)):
            with self.subTest(size=(width, height)):
                screen = StrictScreen(width, height)
                self.assertTrue(app._draw(screen))
                lines = screen.lines()
                frame_width = width
                frame_height = height
                x = y = 0
                self.assertEqual(lines[y][x], "╭")
                self.assertEqual(lines[y][x + frame_width - 1], "╮")
                self.assertEqual(lines[y + frame_height - 1][x], "╰")
                self.assertEqual(lines[y + frame_height - 1][x + frame_width - 1], "╯")
                visible = "\n".join(lines)
                self.assertIn("KEYBOARD", visible)
                self.assertTrue("Q quit" in visible or "Q  quit" in visible)
