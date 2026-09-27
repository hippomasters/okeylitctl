import unittest

from okeylitctl.models import ColorLayout, Zone
from okeylitctl.validation import ValidationError, normalize_color


class ColorLayoutTests(unittest.TestCase):
    def test_wire_order_is_right_center_left_wasd(self):
        layout = ColorLayout.from_wire("110000,002200,000033,444444")

        self.assertEqual(layout.right, "110000")
        self.assertEqual(layout.center, "002200")
        self.assertEqual(layout.left, "000033")
        self.assertEqual(layout.wasd, "444444")
        self.assertEqual(layout.to_wire(), "110000,002200,000033,444444")

    def test_layout_normalizes_each_named_zone(self):
        layout = ColorLayout(
            right="aa0000",
            center="00bb00",
            left="0000cc",
            wasd="dddddd",
        )

        self.assertEqual(layout.to_wire(), "AA0000,00BB00,0000CC,DDDDDD")

    def test_with_zone_returns_a_new_complete_layout(self):
        original = ColorLayout.from_wire("111111,222222,333333,444444")

        changed = original.with_zone(Zone.WASD, "abcdef")

        self.assertEqual(original.to_wire(), "111111,222222,333333,444444")
        self.assertEqual(changed.to_wire(), "111111,222222,333333,ABCDEF")

    def test_single_color_validation_has_one_canonical_format(self):
        self.assertEqual(normalize_color("a1b2c3"), "A1B2C3")
        for invalid in ("", "fff", "GG0000", "#FF0000", "FF000000"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValidationError):
                    normalize_color(invalid)


if __name__ == "__main__":
    unittest.main()
