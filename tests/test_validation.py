import unittest

from okeylitctl.validation import ValidationError, normalize_colors


class ColorValidationTests(unittest.TestCase):
    def test_normalizes_exactly_four_rgb_values(self):
        self.assertEqual(
            normalize_colors("ff0000,00ff00,0000ff,ffffff"),
            "FF0000,00FF00,0000FF,FFFFFF",
        )

    def test_rejects_malformed_color_values(self):
        invalid = [
            "FF0000,00FF00,0000FF",
            "FF0000,00FF00,0000FF,FFFFFF,123456",
            "#FF0000,00FF00,0000FF,FFFFFF",
            "0xFF0000,00FF00,0000FF,FFFFFF",
            "FF0000 00FF00 0000FF FFFFFF",
            "GG0000,00FF00,0000FF,FFFFFF",
            "FF0000,00FF00,0000FF,FFFFF",
            "FF0000,00FF00,0000FF,FFFFFF\nON",
            "ＦＦ0000,00FF00,0000FF,FFFFFF",
        ]
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    normalize_colors(value)
if __name__ == "__main__":
    unittest.main()
