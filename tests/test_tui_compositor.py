import unittest
from colorsys import rgb_to_hsv

from okeylitctl.effects import EffectKind
from okeylitctl.keyboard_layout import project_block_keyboard
from okeylitctl.models import ColorLayout, Zone
from okeylitctl.tui_compositor import (
    _Canvas,
    compose_editor,
    compose_exact_hex_modal,
    compose_profiles,
)
from okeylitctl.tui_state import TuiState


class EditorCompositorTests(unittest.TestCase):
    def test_canvas_rejects_out_of_bounds_writes_instead_of_clipping(self):
        canvas = _Canvas(5, 2)

        with self.assertRaisesRegex(ValueError, "outside canvas"):
            canvas.put(0, 4, "XX")
        with self.assertRaisesRegex(ValueError, "outside canvas"):
            canvas.put(-1, 0, "X")

    def setUp(self):
        self.state = TuiState(
            current=ColorLayout.from_wire("7B2CFF,00CFFF,FF2E88,E8F7FF"),
            original=ColorLayout.from_wire("7B2CFF,00CFFF,FF2E88,E8F7FF"),
        )

    def test_reference_editor_matches_approved_panel_composition(self):
        composed = compose_editor(self.state, power_state="on", version="0.2.0")

        self.assertEqual((composed.width, composed.height), (160, 55))
        self.assertTrue(all(len(line) == 160 for line in composed.lines))
        self.assertIn("◆ okeylitctl v0.2.0", composed.lines[1])
        self.assertIn("IN SYNC", composed.lines[1])
        self.assertIn("DEVICE ON", composed.lines[1])

        self.assertIn("KEYBOARD", composed.lines[4])
        self.assertIn("ZONES", composed.lines[4])
        self.assertIn("LIVE ▸ DRAFT", composed.lines[14])
        self.assertIn("EFFECT", composed.lines[25])
        self.assertIn("1/16", composed.lines[25])
        self.assertIn("COLOR · RIGHT", composed.lines[36])
        self.assertIn("MOTION", composed.lines[36])

        visible = "\n".join(composed.lines)
        for key in ("Esc", "F12", "Pwr", "Del", "◆", "CALC", "Ins", "PRT"):
            self.assertIn(key, visible)
        for effect in (
            "STATIC", "BLINK", "BREATHE", "PULSE", "STROBE", "CYCLE", "WAVE", "GRADIENT",
            "REACTIVE", "RIPPLE", "RAIN", "FIRE", "AURORA", "SPARKLE", "COMET", "SCANNER",
        ):
            self.assertIn(effect, visible)
        for forbidden in ("PHOTO-MATCHED", "APPROX.", "NOT PER-KEY", "SAFE KEYBOARD LIGHTING"):
            self.assertNotIn(forbidden, visible)

    def test_keyboard_top_and_single_row_keycaps_have_distinct_composed_roles(self):
        composed = compose_editor(self.state, power_state="on", version="0.2.0")
        projection = project_block_keyboard(103, 19)
        tall = next(item for item in projection.keys if item.key.id == "Q")
        single = next(item for item in projection.keys if item.key.id == "LEFT")

        # The reference keyboard's projected origin is (4, 5).
        self.assertEqual(composed.lines[5 + tall.y][4 + tall.x], "▀")
        self.assertEqual(
            composed.roles[5 + tall.y][4 + tall.x], f"key_{tall.zone.value}_top"
        )
        self.assertEqual(
            composed.roles[5 + tall.bottom - 1][4 + tall.x],
            f"key_{tall.zone.value}",
        )
        self.assertNotIn(
            "_top", composed.roles[5 + single.y][4 + single.x]
        )

    def test_profiles_keyboard_uses_saved_colors_with_separate_top_and_body(self):
        saved = ColorLayout.from_wire("FF5E5A,FF9E3D,FFD23F,FFF1D6")
        composed = compose_profiles(
            self.state, names=("Sunset",), selected=0,
            preview_layout=saved, power_state="on", version="0.2.0",
        )
        projection = project_block_keyboard(100, 19)
        key = next(item for item in projection.keys if item.key.id == "Q")
        origin_x, origin_y = 55, 6
        self.assertEqual(composed.lines[origin_y + key.y][origin_x + key.x], "▀")
        self.assertEqual(
            composed.roles[origin_y + key.y][origin_x + key.x],
            f"profile_key_{key.zone.value}_top_{getattr(saved, key.zone.value)}",
        )
        self.assertEqual(
            composed.roles[origin_y + key.bottom - 1][origin_x + key.x],
            f"profile_key_{key.zone.value}_{getattr(saved, key.zone.value)}",
        )

    def test_zones_live_draft_color_motion_and_footer_match_mockup(self):
        composed = compose_editor(self.state, power_state="on", version="0.2.0")
        visible = "\n".join(composed.lines)

        for text in (
            "1  RIGHT", "#7B2CFF", "2  CENTER", "#00CFFF",
            "3  LEFT", "#FF2E88", "4  WASD", "#E8F7FF",
            "LIVE", "DRAFT", "No changes",
            "RED", "GREEN", "BLUE", "PRESET", "Aurora",
            "SPEED", "LIGHT", "DIR",
            "zone", "channel", "adjust", "effect", "speed", "hex", "preset",
            "apply", "discard", "original", "profiles", "refresh", "help", "quit",
        ):
            with self.subTest(text=text):
                self.assertIn(text, visible)

    def test_dirty_selected_zone_changes_badge_and_live_draft_values(self):
        self.state.selected_zone = Zone.WASD
        self.state.set_selected_color("4DF0C8")

        composed = compose_editor(self.state, power_state="on", version="0.2.0")
        visible = "\n".join(composed.lines)

        self.assertIn("UNSAVED DRAFT", composed.lines[1])
        self.assertIn("COLOR · WASD", composed.lines[36])
        self.assertIn("#E8F7FF", visible)
        self.assertIn("#4DF0C8", visible)
        self.assertIn("R-155", visible)
        self.assertIn("G-7", visible)
        self.assertIn("B-55", visible)
        self.assertEqual(composed.roles[1][112], "dirty_badge")
        self.assertEqual(composed.roles[17][128], "swatch_live_wasd_E8F7FF")
        self.assertEqual(composed.roles[18][128], "swatch_draft_wasd_4DF0C8")

    def test_running_effect_displays_verified_live_frame_not_saved_base(self):
        self.state.effect_running = True
        self.state.effect_frame = ColorLayout.from_wire(
            "AA0000,00BB00,0000CC,DDDD00"
        )
        self.state.selected_zone = Zone.RIGHT

        composed = compose_editor(self.state, power_state="on", version="0.2.0")
        visible = "\n".join(composed.lines)

        self.assertIn("EFFECT ACTIVE", composed.lines[1])
        self.assertNotIn("IN SYNC", composed.lines[1])
        self.assertIn("#AA0000", visible)
        self.assertIn(f"#{self.state.draft.right}", visible)
        self.assertIn("Effect frame active", visible)
        self.assertEqual(composed.roles[17][128], "swatch_live_right_AA0000")

    def test_keyboard_uses_only_last_verified_effect_frame_while_running(self):
        self.state.effect_running = True
        self.state.effect_frame = ColorLayout.from_wire(
            "AA0000,00BB00,0000CC,DDDD00"
        )
        from okeylitctl.tui_compositor import (
            compose_adaptive_editor, compose_narrow_editor,
        )
        for composed in (
            compose_editor(self.state, power_state="on", version="0.2.0"),
            compose_adaptive_editor(self.state, width=110, height=30, power_state="on"),
            compose_narrow_editor(self.state, width=78, height=24, power_state="on"),
        ):
            with self.subTest(size=(composed.width, composed.height)):
                roles = {role for row in composed.roles for role in row if role.startswith("key_")}
                self.assertTrue(any("AA0000" in role for role in roles))
                self.assertFalse(any("7B2CFF" in role for role in roles))

        self.state.effect_frame_uncertain = True
        for composed in (
            compose_editor(self.state, power_state="on"),
            compose_adaptive_editor(self.state, width=110, height=30, power_state="on"),
            compose_narrow_editor(self.state, width=78, height=24, power_state="on"),
        ):
            roles = {role for row in composed.roles for role in row if role.startswith("key_")}
            self.assertFalse(any("AA0000" in role or "7B2CFF" in role for role in roles))

    def test_uncertain_effect_state_never_labels_stale_frame_as_live(self):
        self.state.effect_running = True
        self.state.effect_frame = ColorLayout.from_wire(
            "AA0000,00BB00,0000CC,DDDD00"
        )
        self.state.effect_frame_uncertain = True
        self.state.selected_zone = Zone.RIGHT

        composed = compose_editor(self.state, power_state="on", version="0.2.0")
        visible = "\n".join(composed.lines)

        self.assertIn("EFFECT UNKNOWN", composed.lines[1])
        self.assertIn("UNKNOWN", visible)
        self.assertIn("Device state unknown", visible)
        self.assertNotIn("#AA0000", visible)
        self.assertNotIn("Effect frame active", visible)

    def test_restore_pending_status_failure_never_claims_cached_frame_is_live(self):
        self.state.effect_running = True
        self.state.effect_restore_pending = True
        self.state.effect_frame = ColorLayout.from_wire(
            "AA0000,00BB00,0000CC,DDDD00"
        )
        self.state.effect_frame_uncertain = True
        self.state.selected_zone = Zone.RIGHT

        composed = compose_editor(self.state, power_state="on", version="0.2.0")
        visible = "\n".join(composed.lines)

        self.assertIn("EFFECT UNKNOWN", composed.lines[1])
        self.assertNotIn("EFFECT ACTIVE", composed.lines[1])
        self.assertIn("UNKNOWN", visible)
        self.assertNotIn("#AA0000", visible)
        self.assertNotIn("Effect frame active", visible)

    def test_power_off_effect_state_never_claims_stale_frame_is_live(self):
        self.state.effect_running = True
        self.state.effect_frame = ColorLayout.from_wire(
            "AA0000,00BB00,0000CC,DDDD00"
        )
        self.state.selected_zone = Zone.RIGHT

        composed = compose_editor(self.state, power_state="off", version="0.2.0")
        visible = "\n".join(composed.lines)

        self.assertIn("RESTORE PENDING", composed.lines[1])
        self.assertIn("DEVICE OFF", composed.lines[1])
        self.assertIn("POWER OFF", visible)
        self.assertIn("Base restore pending", visible)
        self.assertNotIn("#AA0000", visible)
        self.assertNotIn("Effect frame active", visible)

    def test_running_effect_exposes_a_visible_stop_shortcut(self):
        self.state.effect_running = True

        composed = compose_editor(self.state, power_state="on", version="0.2.0")
        footer = "\n".join(composed.lines[49:53])

        self.assertIn("S Stop", footer)

    def test_effect_selection_updates_grid_index_and_motion_controls(self):
        self.state.effect_index = tuple(EffectKind).index(EffectKind.RIPPLE)
        self.state.effect_speed = 0.6
        self.state.effect_light = 0.8
        self.state.selected_zone = Zone.WASD

        composed = compose_editor(self.state, power_state="on", version="0.2.0")
        visible = "\n".join(composed.lines)

        self.assertIn("10/16", composed.lines[25])
        self.assertIn("COLOR · WASD", composed.lines[36])
        self.assertIn("60%", visible)
        self.assertIn("80%", visible)
        ripple_cells = [
            composed.roles[row][column]
            for row in range(composed.height)
            for column in range(composed.width)
            if composed.lines[row][column : column + 6] == "RIPPLE"
        ]
        self.assertIn("effect_selected", ripple_cells)

    def test_cycle_uses_the_spectrum_color_panel_from_the_mockup(self):
        self.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        self.state.effect_speed = 0.45
        self.state.effect_light = 1.0

        composed = compose_editor(self.state, power_state="on", version="0.2.0")
        visible = "\n".join(composed.lines)

        self.assertIn("6/16", composed.lines[25])
        self.assertIn("COLOR · SPECTRUM", composed.lines[36])
        self.assertIn("FROM", visible)
        self.assertIn("TO", visible)
        self.assertIn("SAT", visible)
        self.assertIn("45%", visible)
        self.assertIn("100%", visible)

    def test_cycle_spectrum_and_effect_strip_contain_multiple_hues(self):
        from okeylitctl.tui_compositor import compose_adaptive_editor

        self.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        for composed in (
            compose_editor(self.state, power_state="on"),
            compose_adaptive_editor(self.state, width=110, height=30, power_state="on"),
        ):
            with self.subTest(size=(composed.width, composed.height)):
                hues = {
                    role for row in composed.roles for role in row
                    if role.startswith("spectrum_")
                }
                self.assertGreaterEqual(len(hues), 6)
                self.assertNotIn("effect_preview_cycle", {
                    role for row in composed.roles for role in row
                })

    def test_default_cycle_spectrum_matches_its_saturation_setting(self):
        self.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        composed = compose_editor(self.state, power_state="on")
        first = next(
            role for row in composed.roles for role in row
            if role.startswith("spectrum_0_")
        )
        color = first.rsplit("_", 1)[1]
        rgb = tuple(int(color[offset:offset + 2], 16) / 255 for offset in (0, 2, 4))
        self.assertAlmostEqual(rgb_to_hsv(*rgb)[1], 0.9, delta=0.01)

    def test_cycle_spectrum_uses_a_fine_gradient_not_seven_large_blocks(self):
        self.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        composed = compose_editor(self.state, power_state="on")
        shades = {
            role for row in composed.roles for role in row
            if role.startswith("spectrum_")
        }
        self.assertGreaterEqual(len(shades), 20)

    def test_cycle_hue_chip_uses_the_selected_hue_not_draft_zone_color(self):
        self.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        self.state.cycle_from_deg = 60
        self.state.cycle_to_deg = 180
        composed = compose_editor(self.state, power_state="on")
        self.assertIn("H  60°", "\n".join(composed.lines))
        self.assertTrue(composed.roles[38][104].startswith("spectrum_0_"))
        self.assertNotEqual(composed.roles[38][104], "swatch_center")

    def test_cycle_preset_chips_show_generated_palette_not_base_colors(self):
        self.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        self.state.cycle_from_deg = 60
        self.state.cycle_to_deg = 180
        self.state.cycle_saturation_percent = 40
        composed = compose_editor(self.state, power_state="on")
        before = [composed.roles[44][44 + index * 7] for index in range(5)]
        self.assertTrue(all(role.startswith("spectrum_") for role in before))
        self.assertFalse(any("7B2CFF" in role or "00CFFF" in role for role in before))
        self.state.cycle_saturation_percent = 90
        changed = compose_editor(self.state, power_state="on")
        after = [changed.roles[44][44 + index * 7] for index in range(5)]
        self.assertNotEqual(before, after)

    def test_cycle_controls_update_values_and_spectrum_at_each_layout(self):
        from okeylitctl.tui_compositor import compose_adaptive_editor, compose_narrow_editor

        self.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        layouts = (
            lambda: compose_editor(self.state, power_state="on"),
            lambda: compose_adaptive_editor(
                self.state, width=110, height=30, power_state="on"
            ),
            lambda: compose_adaptive_editor(
                self.state, width=159, height=54, power_state="on"
            ),
            lambda: compose_narrow_editor(
                self.state, width=78, height=24, power_state="on"
            ),
            lambda: compose_narrow_editor(
                self.state, width=78, height=55, power_state="on"
            ),
        )
        baseline = [
            {role for row in render().roles for role in row if role.startswith("spectrum_")}
            for render in layouts
        ]
        self.state.selected_channel = 0
        self.state.adjust_cycle_control(1)
        self.state.selected_channel = 1
        self.state.adjust_cycle_control(-1)
        self.state.selected_channel = 2
        self.state.adjust_cycle_control(-1)
        for render, before in zip(layouts, baseline):
            composed = render()
            with self.subTest(size=(composed.width, composed.height)):
                visible = "\n".join(composed.lines)
                for label in ("FROM", "TO", "SAT", "5°", "355°", "85%"):
                    self.assertIn(label, visible)
                after = {
                    role for row in composed.roles for role in row
                    if role.startswith("spectrum_")
                }
                self.assertNotEqual(after, before)

    def test_custom_cycle_range_does_not_claim_rainbow_preset(self):
        from okeylitctl.tui_compositor import compose_narrow_editor

        self.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        self.state.adjust_cycle_control(1)
        for composed in (
            compose_editor(self.state, power_state="on"),
            compose_narrow_editor(self.state, width=78, height=38, power_state="on"),
        ):
            with self.subTest(size=(composed.width, composed.height)):
                visible = "\n".join(composed.lines)
                self.assertIn("PRESET  ‹ Custom ›", visible)
                self.assertNotIn("PRESET  ‹ Rainbow ›", visible)

    def test_selected_cycle_preset_name_matches_its_range(self):
        from okeylitctl.tui_compositor import compose_narrow_editor

        self.state.effect_index = tuple(EffectKind).index(EffectKind.CYCLE)
        self.assertEqual(self.state.select_next_cycle_preset(), "Warm")
        for composed in (
            compose_editor(self.state, power_state="on"),
            compose_narrow_editor(self.state, width=78, height=38, power_state="on"),
        ):
            self.assertIn("PRESET  ‹ Warm ›", "\n".join(composed.lines))

    def test_static_effect_strip_shows_four_zone_colors_not_flat_teal(self):
        from okeylitctl.tui_compositor import compose_adaptive_editor

        reference = compose_editor(self.state, power_state="on")
        self.state.compact_panel = "effects"
        for composed in (
            reference,
            compose_adaptive_editor(self.state, width=110, height=30, power_state="on"),
        ):
            with self.subTest(size=(composed.width, composed.height)):
                colors = {
                    role for row in composed.roles for role in row
                    if role.startswith("effect_color_")
                }
                self.assertEqual(len(colors), 4)
                self.assertNotIn("effect_preview_static", {
                    role for row in composed.roles for role in row
                })

    def test_exact_hex_modal_dims_editor_and_explains_incomplete_input(self):
        self.state.selected_zone = Zone.WASD
        composed = compose_exact_hex_modal(
            self.state,
            power_state="on",
            version="0.2.0",
            input_text="FF9E3",
            error="needs 1 more digit",
        )
        visible = "\n".join(composed.lines)

        self.assertIn("EXACT HEX · WASD", visible)
        self.assertIn("# FF9E3", visible)
        self.assertIn("needs 1 more digit", visible)
        self.assertIn("LIVE", visible)
        self.assertIn("#E8F7FF", visible)
        self.assertIn("NEW", visible)
        self.assertIn("waiting…", visible)
        self.assertIn("Enter", visible)
        self.assertIn("accept", visible)
        self.assertIn("Esc", visible)
        self.assertIn("cancel", visible)
        self.assertIn("^U", visible)
        self.assertIn("clear", visible)
        self.assertTrue(composed.roles[4][6].startswith("dimmed_"))
        self.assertEqual(composed.roles[17][43], "modal_border")
        self.assertEqual(composed.roles[20][49], "modal_input")

    def test_exact_hex_modal_previews_a_valid_new_color(self):
        self.state.selected_zone = Zone.WASD
        composed = compose_exact_hex_modal(
            self.state,
            power_state="on",
            version="0.2.0",
            input_text="4DF0C8",
            error="",
        )
        visible = "\n".join(composed.lines)

        self.assertIn("#4DF0C8", visible)
        self.assertNotIn("waiting…", visible)
        self.assertIn("ready", visible)
        roles = {role for row in composed.roles for role in row}
        self.assertIn("modal_swatch_live_E8F7FF", roles)
        self.assertIn("modal_swatch_new_4DF0C8", roles)

    def test_reference_profiles_matches_split_list_and_preview_composition(self):
        preview = ColorLayout.from_wire("FF6B35,FFB000,7A001F,FFF1D0")

        composed = compose_profiles(
            self.state,
            names=("Sunset", "Work"),
            selected=0,
            preview_layout=preview,
            power_state="on",
            version="0.2.0",
            message="Profile ready",
        )
        visible = "\n".join(composed.lines)

        self.assertEqual((composed.width, composed.height), (160, 43))
        self.assertTrue(all(len(line) == 160 for line in composed.lines))
        self.assertIn("◆ okeylitctl v0.2.0  ›  Profiles", composed.lines[1])
        self.assertIn("IN SYNC", composed.lines[1])
        self.assertIn("DEVICE ON", composed.lines[1])
        self.assertIn("PROFILES", composed.lines[4])
        self.assertIn("PREVIEW · SUNSET", composed.lines[4])
        self.assertIn("▸ Sunset", visible)
        self.assertIn("Work", visible)
        self.assertIn("#FF6B35", visible)
        self.assertIn("#FFB000", visible)
        self.assertIn("#7A001F", visible)
        self.assertIn("#FFF1D0", visible)
        role_names = {role for row in composed.roles for role in row}
        for zone, color in (
            ("right", "FF6B35"),
            ("center", "FFB000"),
            ("left", "7A001F"),
            ("wasd", "FFF1D0"),
        ):
            self.assertIn(f"profile_color_{zone}_{color}", role_names)
        for key in ("Esc", "F12", "Pwr", "Del", "◆", "CALC", "Ins", "PRT"):
            self.assertIn(key, visible)
        for action in (
            "select",
            "load into draft",
            "save current as",
            "rename",
            "delete",
            "back",
        ):
            self.assertIn(action, visible)
        selected_cells = {
            composed.roles[row][column]
            for row in range(composed.height)
            for column in range(composed.width)
            if composed.lines[row][column] == "▸"
        }
        self.assertIn("profile_selected", selected_cells)

    def test_profiles_delete_confirmation_preserves_panel_border_for_max_name(self):
        name = "A" * 32
        composed = compose_profiles(
            self.state,
            names=(name,),
            selected=0,
            preview_layout=self.state.draft,
            pending_delete=name,
            power_state="on",
            version="0.2.0",
        )

        self.assertIn("Delete profile?", composed.lines[33])
        self.assertIn(name, composed.lines[34])
        self.assertIn("Y confirm · N cancel", composed.lines[35])
        for row in range(5, 36):
            self.assertEqual(composed.lines[row][48], "│")

    def test_profiles_preview_error_is_visible_without_fabricating_a_layout(self):
        composed = compose_profiles(
            self.state,
            names=("Broken",),
            selected=0,
            preview_layout=None,
            preview_error="profile store is malformed",
            power_state="on",
            version="0.2.0",
            message="",
        )
        visible = "\n".join(composed.lines)

        self.assertIn("PREVIEW · BROKEN", composed.lines[4])
        self.assertIn("Preview unavailable", visible)
        self.assertIn("profile store is malformed", visible)
        self.assertNotIn("#000000", visible)


if __name__ == "__main__":
    unittest.main()
