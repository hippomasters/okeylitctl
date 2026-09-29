import unittest

from okeylitctl.models import Zone
from okeylitctl.keyboard_layout import (
    KEYBOARD_KEYS,
    KEYBOARD_ROWS,
    key_zone,
    project_keyboard,
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
