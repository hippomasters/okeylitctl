import unittest

from okeylitctl.tui_visual_contract import (
    EDITOR_LAYOUT,
    EFFECT_NAMES,
    FOOTER_ROWS,
    HEADER,
    HEX_MODAL,
    OUTER_FRAME,
    PALETTE,
    PROFILES_LAYOUT,
    REFERENCE_STATES,
    render_contract_snapshot,
)


class TuiVisualContractTests(unittest.TestCase):
    def test_editor_reference_canvas_matches_mockup_aspect_and_panel_grid(self):
        self.assertEqual((EDITOR_LAYOUT.width, EDITOR_LAYOUT.height), (160, 55))
        self.assertEqual(EDITOR_LAYOUT.panel("keyboard").as_tuple(), (6, 4, 107, 21))
        self.assertEqual(EDITOR_LAYOUT.panel("zones").as_tuple(), (116, 4, 40, 9))
        self.assertEqual(EDITOR_LAYOUT.panel("live_draft").as_tuple(), (116, 14, 40, 11))
        self.assertEqual(EDITOR_LAYOUT.panel("effect").as_tuple(), (6, 25, 150, 11))
        self.assertEqual(EDITOR_LAYOUT.panel("color").as_tuple(), (6, 36, 107, 11))
        self.assertEqual(EDITOR_LAYOUT.panel("motion").as_tuple(), (116, 36, 40, 11))
        self.assertEqual(EDITOR_LAYOUT.panel("footer").as_tuple(), (5, 49, 151, 4))

    def test_editor_panels_are_inside_canvas_and_do_not_overlap(self):
        panels = tuple(EDITOR_LAYOUT.panels.values())
        for panel in panels:
            self.assertGreaterEqual(panel.x, 0)
            self.assertGreaterEqual(panel.y, 0)
            self.assertLessEqual(panel.right, EDITOR_LAYOUT.width)
            self.assertLessEqual(panel.bottom, EDITOR_LAYOUT.height)
        for index, left in enumerate(panels):
            for right in panels[index + 1 :]:
                self.assertFalse(left.overlaps(right), f"{left.name} overlaps {right.name}")

    def test_exact_hex_modal_matches_centered_mockup_geometry(self):
        self.assertEqual(HEX_MODAL.as_tuple(), (43, 17, 74, 18))
        self.assertEqual(HEX_MODAL.x + HEX_MODAL.width // 2, EDITOR_LAYOUT.width // 2)
        self.assertEqual(HEX_MODAL.y + HEX_MODAL.height // 2, EDITOR_LAYOUT.height // 2 - 1)

    def test_outer_frame_header_and_footer_are_explicit(self):
        self.assertEqual(OUTER_FRAME.as_tuple(), (0, 0, 160, 55))
        self.assertEqual(HEADER.as_tuple(), (3, 1, 154, 2))
        self.assertEqual(
            FOOTER_ROWS,
            (
                "zone | channel | adjust | effect | speed | hex | preset",
                "apply | discard | original | profiles | refresh | help | quit",
            ),
        )

    def test_reference_states_capture_each_supplied_editor_composition(self):
        self.assertEqual(REFERENCE_STATES["cycle_synced"]["effect"], "Cycle")
        self.assertEqual(REFERENCE_STATES["cycle_synced"]["effect_index"], "6/16")
        self.assertEqual(REFERENCE_STATES["cycle_synced"]["status"], "IN SYNC")
        self.assertEqual(REFERENCE_STATES["ripple_dirty"]["effect"], "Ripple")
        self.assertEqual(REFERENCE_STATES["ripple_dirty"]["effect_index"], "10/16")
        self.assertEqual(REFERENCE_STATES["ripple_dirty"]["status"], "UNSAVED DRAFT")
        self.assertEqual(REFERENCE_STATES["hex_invalid"]["input"], "#FF9E3")
        self.assertEqual(REFERENCE_STATES["hex_invalid"]["error"], "needs 1 more digit")
        self.assertEqual(REFERENCE_STATES["profiles"]["selected"], "Sunset")

    def test_reference_snapshot_fixtures_match_contract_renderer(self):
        for name, layout in (
            ("editor", EDITOR_LAYOUT),
            ("profiles", PROFILES_LAYOUT),
        ):
            with self.subTest(name=name):
                fixture = (
                    __import__("pathlib").Path(__file__).parent
                    / "fixtures"
                    / f"tui_{name}_contract.txt"
                ).read_text(encoding="utf-8").rstrip("\n")
                self.assertEqual(render_contract_snapshot(layout), fixture)

    def test_profiles_uses_its_shorter_reference_canvas(self):
        self.assertEqual((PROFILES_LAYOUT.width, PROFILES_LAYOUT.height), (160, 43))
        self.assertEqual(PROFILES_LAYOUT.panel("profiles").as_tuple(), (6, 4, 43, 33))
        self.assertEqual(PROFILES_LAYOUT.panel("preview").as_tuple(), (53, 4, 104, 33))
        self.assertEqual(PROFILES_LAYOUT.panel("footer").as_tuple(), (5, 39, 152, 2))

    def test_effect_grid_names_and_order_match_the_mockups(self):
        self.assertEqual(
            EFFECT_NAMES,
            (
                "Static", "Blink", "Breathe", "Pulse", "Strobe", "Cycle", "Wave", "Gradient",
                "Reactive", "Ripple", "Rain", "Fire", "Aurora", "Sparkle", "Comet", "Scanner",
            ),
        )

    def test_palette_preserves_mockup_semantics(self):
        self.assertEqual(PALETTE["background"], "#0B0E18")
        self.assertEqual(PALETTE["panel"], "#111522")
        self.assertEqual(PALETTE["border"], "#30364C")
        self.assertEqual(PALETTE["text"], "#D7DCEF")
        self.assertEqual(PALETTE["muted"], "#737A93")
        self.assertEqual(PALETTE["focus"], "#63DED3")
        self.assertEqual(PALETTE["synced"], "#51D88A")
        self.assertEqual(PALETTE["dirty"], "#F5C85B")
        for role in (
            "header_fill",
            "selection_fill",
            "profile_selection_fill",
            "shortcut_fill",
            "modal_fill",
            "overlay_scrim",
            "disabled",
            "error",
            "channel_red",
            "channel_green",
            "channel_blue",
            "bright_text",
            "dark_text",
        ):
            with self.subTest(role=role):
                self.assertIn(role, PALETTE)


if __name__ == "__main__":
    unittest.main()
