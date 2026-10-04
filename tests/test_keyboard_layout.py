import unittest

from okeylitctl.models import Zone
from okeylitctl.keyboard_layout import (
    KEYBOARD_KEYS,
    KEYBOARD_ROWS,
    block_key_legend,
    key_zone,
    project_block_keyboard,
    project_keyboard,
    render_block_keyboard,
)


class KeyboardGeometryTests(unittest.TestCase):
    def test_photo_layout_contains_100_unique_physical_keys(self):
        self.assertEqual(len(KEYBOARD_KEYS), 100)
        self.assertEqual(len({key.id for key in KEYBOARD_KEYS}), 100)

    def test_rows_preserve_visible_physical_order(self):
        self.assertEqual(
            [key.legend for key in KEYBOARD_ROWS["function"]],
            [
                "ESC", "F1", "F2", "F3", "F4", "F5", "F6", "F7",
                "F8", "F9", "F10", "F11", "F12", "PWR", "DEL", "<>",
                "CALC", "INS", "PRTSC",
            ],
        )
        self.assertEqual(
            [key.legend for key in KEYBOARD_ROWS["number"]],
            [
                "`", "1", "2", "3", "4", "5", "6", "7", "8", "9",
                "0", "-", "=", "BKSP", "NUMLK", "/", "*", "-",
            ],
        )
        self.assertEqual(
            [key.legend for key in KEYBOARD_ROWS["qwerty"][-4:]],
            ["7", "8", "9", "+"],
        )

    def test_wide_and_spanning_keys_match_the_photo(self):
        by_id = {key.id: key for key in KEYBOARD_KEYS}
        self.assertEqual(by_id["BACKSPACE"].width, 2.0)
        self.assertEqual(by_id["SPACE"].width, 6.25)
        self.assertEqual(by_id["KP0"].width, 2.0)
        self.assertEqual(by_id["KP_ADD"].height, 2.10)
        self.assertEqual(by_id["KP_ENTER"].height, 2.10)
        self.assertEqual(by_id["UP"].height, 0.48)
        self.assertGreater(by_id["DOWN"].y, by_id["UP"].y)

    def test_special_key_legends_are_unambiguous_in_block_renderer(self):
        by_id = {key.id: key for key in KEYBOARD_KEYS}
        self.assertEqual(block_key_legend(by_id["OMEN"], 5), "◆")
        self.assertEqual(block_key_legend(by_id["CALCULATOR"], 5), "CALC")
        self.assertEqual(block_key_legend(by_id["PRINT"], 5), "PRT")
        self.assertEqual(block_key_legend(by_id["NUMLOCK"], 5), "NUM")
        self.assertEqual(block_key_legend(by_id["BACKSPACE"], 8), "Bksp")

    def test_block_projection_fills_reference_panel_without_repacking_photo(self):
        projection = project_block_keyboard(103, 19)
        self.assertEqual((projection.width, projection.height), (103, 19))
        self.assertEqual(len(projection.keys), 100)
        self.assertGreaterEqual(projection.used_width, 98)
        self.assertLessEqual(projection.used_width, 103)
        self.assertEqual(projection.unit_x, 5)
        self.assertEqual(projection.unit_y, 3)

        by_id = {item.key.id: item for item in projection.keys}
        self.assertEqual(by_id["UP"].x, by_id["DOWN"].x)
        self.assertLess(by_id["UP"].y, by_id["DOWN"].y)
        self.assertLess(by_id["LEFT"].x, by_id["DOWN"].x)
        self.assertLess(by_id["DOWN"].x, by_id["RIGHT"].x)
        self.assertLess(by_id["RIGHT"].right, by_id["KP0"].x)
        self.assertGreater(by_id["KP_ADD"].height, by_id["KP7"].height)
        self.assertGreater(by_id["KP_ENTER"].height, by_id["KP3"].height)

    def test_wide_keyboard_projection_uses_available_panel_width(self):
        for width in (68, 103, 151):
            with self.subTest(width=width):
                projection = project_block_keyboard(width, 19)
                self.assertLessEqual(min(key.x for key in projection.keys), 3)
                self.assertGreaterEqual(max(key.right for key in projection.keys), width - 4)
                self.assertTrue(all(key.right <= width for key in projection.keys))

    def test_keyboard_rows_do_not_have_double_blank_gaps(self):
        projection = project_block_keyboard(103, 19)
        occupied = {
            row for key in projection.keys for row in range(key.y, key.bottom)
        }
        for row in range(max(occupied)):
            self.assertTrue(row in occupied or row + 1 in occupied)

    def test_block_projection_rectangles_are_in_bounds_and_do_not_overlap(self):
        projection = project_block_keyboard(103, 19)
        for item in projection.keys:
            self.assertGreaterEqual(item.x, 0)
            self.assertGreaterEqual(item.y, 0)
            self.assertLessEqual(item.right, projection.width)
            self.assertLessEqual(item.bottom, projection.height)
        for index, left in enumerate(projection.keys):
            for right in projection.keys[index + 1 :]:
                overlaps = (
                    left.x < right.right
                    and right.x < left.right
                    and left.y < right.bottom
                    and right.y < left.bottom
                )
                self.assertFalse(overlaps, f"{left.key.id} overlaps {right.key.id}")

    def test_block_renderer_preserves_key_identity_zone_and_selected_treatment(self):
        rendered = render_block_keyboard(103, 19, Zone.WASD)
        self.assertEqual(len(rendered.lines), 19)
        self.assertTrue(all(len(line) == 103 for line in rendered.lines))
        self.assertEqual(len(rendered.key_ids), 19)
        self.assertEqual(len(rendered.zones), 19)

        selected_ids = {
            rendered.key_ids[row][column]
            for row in range(rendered.height)
            for column in range(rendered.width)
            if rendered.selected[row][column]
        }
        self.assertEqual(selected_ids, {"W", "A", "S", "D"})

        visible = "\n".join(rendered.lines)
        for legend in ("Esc", "F12", "Pwr", "Del", "◆", "CALC", "Ins", "PRT"):
            self.assertIn(legend, visible)
        self.assertIn("Bksp", visible)
        self.assertIn("Shift", visible)

    def test_block_renderer_uses_dark_body_and_top_highlight_instead_of_full_blocks(self):
        projection = project_block_keyboard(103, 19)
        rendered = render_block_keyboard(103, 19, Zone.RIGHT)
        q_key = next(item for item in projection.keys if item.key.id == "Q")

        self.assertGreaterEqual(q_key.height, 2)
        self.assertEqual(
            rendered.lines[q_key.y][q_key.x : q_key.right],
            "▀" * q_key.width,
        )
        body = rendered.lines[q_key.bottom - 1][q_key.x : q_key.right]
        self.assertIn("Q", body)
        self.assertNotIn("█", body)
        self.assertNotIn("█", "\n".join(rendered.lines))

    def test_zone_shading_is_coarse_and_wasd_only_overrides_four_keys(self):
        by_id = {key.id: key for key in KEYBOARD_KEYS}
        self.assertEqual(key_zone(by_id["W"]), Zone.WASD)
        self.assertEqual(key_zone(by_id["A"]), Zone.WASD)
        self.assertEqual(key_zone(by_id["S"]), Zone.WASD)
        self.assertEqual(key_zone(by_id["D"]), Zone.WASD)
        self.assertEqual(key_zone(by_id["Q"]), Zone.LEFT)
        self.assertEqual(key_zone(by_id["G"]), Zone.CENTER)
        self.assertEqual(key_zone(by_id["RIGHT"]), Zone.RIGHT)
        self.assertEqual(key_zone(by_id["KP_ENTER"]), Zone.RIGHT)

    def test_photo_verified_boundary_keys_belong_to_center_zone(self):
        by_id = {key.id: key for key in KEYBOARD_KEYS}
        for key_id in (
            "F12",
            "EQUAL",
            "RBRACKET",
            "APOSTROPHE",
            "SLASH",
            "RCTRL",
        ):
            with self.subTest(key_id=key_id):
                self.assertEqual(key_zone(by_id[key_id]), Zone.CENTER)
        self.assertEqual(key_zone(by_id["LCTRL"]), Zone.LEFT)

    def test_projection_is_responsive_and_keeps_perspective_and_spans(self):
        compact = project_keyboard(78)
        wide = project_keyboard(120)

        self.assertEqual(compact.unit_width, 3)
        self.assertEqual(wide.unit_width, 5)
        self.assertGreater(compact.row_origins["function"], compact.row_origins["bottom"])
        self.assertLess(compact.width, 78)
        self.assertLess(wide.width, 120)

        compact_by_id = {item.key.id: item for item in compact.keys}
        self.assertEqual(compact_by_id["KP_ADD"].row_span, 2)
        self.assertEqual(compact_by_id["KP_ENTER"].row_span, 2)
        self.assertEqual(compact_by_id["UP"].screen_row, compact_by_id["DOWN"].screen_row - 1)

    def test_projected_keycaps_do_not_overlap_in_any_render_row(self):
        for terminal_width in (78, 110, 140):
            with self.subTest(terminal_width=terminal_width):
                projection = project_keyboard(terminal_width)
                for screen_row in range(7):
                    row = sorted(
                        (
                            item
                            for item in projection.keys
                            if item.screen_row == screen_row
                        ),
                        key=lambda item: item.screen_x,
                    )
                    for left, right in zip(row, row[1:]):
                        self.assertLessEqual(
                            left.screen_x + left.width,
                            right.screen_x,
                            f"{left.key.id} overlaps {right.key.id}",
                        )

    def test_source_key_rectangles_do_not_physically_overlap(self):
        for index, left in enumerate(KEYBOARD_KEYS):
            for right in KEYBOARD_KEYS[index + 1 :]:
                overlaps_x = (
                    left.x < right.x + right.width
                    and right.x < left.x + left.width
                )
                overlaps_y = (
                    left.y < right.y + right.height
                    and right.y < left.y + left.height
                )
                self.assertFalse(
                    overlaps_x and overlaps_y,
                    f"{left.id} overlaps {right.id}",
                )

    def test_projection_preserves_declared_arrow_coordinates(self):
        projection = project_keyboard(78)
        by_id = {item.key.id: item for item in projection.keys}
        for key_id in ("LEFT", "UP", "DOWN", "RIGHT"):
            item = by_id[key_id]
            expected = (
                projection.row_origins["bottom"]
                + int(item.key.x * projection.unit_width + 0.5)
            )
            self.assertEqual(item.screen_x, expected)
        self.assertEqual(
            len({by_id[key_id].width for key_id in ("LEFT", "DOWN", "RIGHT")}),
            1,
        )


if __name__ == "__main__":
    unittest.main()
