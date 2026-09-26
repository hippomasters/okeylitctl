import re
import unittest
from pathlib import Path


SOURCE = Path(__file__).parents[1] / "module" / "omen_rgb.c"


class KernelSourceSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SOURCE.read_text(encoding="utf-8")

    def test_has_exact_initial_hardware_allowlist(self):
        for value in (
            '"HP"',
            '"OMEN by HP Gaming Laptop 16-wf0xxx"',
            '"8BAB"',
            '"B21E3PA#ACJ"',
            '"F.26"',
        ):
            self.assertIn(value, self.source)
        self.assertNotIn("force_unsupported", self.source)

    def test_state_is_read_only_and_has_no_firmware_set_path(self):
        self.assertNotIn("HPWMI_STATE_SET_QUERY", self.source)
        self.assertNotIn("state_store", self.source)
        self.assertRegex(self.source, r"__ATTR\(state,\s*0400,\s*state_show,\s*NULL\)")

    def test_has_no_raw_firmware_escape_hatch(self):
        forbidden = ("debugfs", "proc_create", "unlocked_ioctl", "module_param")
        for token in forbidden:
            with self.subTest(token=token):
                self.assertNotIn(token, self.source)

    def test_mutating_sysfs_attributes_are_admin_only(self):
        self.assertRegex(self.source, r"__ATTR\(colors,\s*0600,")
        self.assertRegex(self.source, r"__ATTR\(restore,\s*0200,")
        self.assertRegex(self.source, r"__ATTR\(original,\s*0444,")
        self.assertRegex(self.source, r"__ATTR\(abi_version,\s*0444,")

    def test_protocol_is_fixed_and_bounded(self):
        self.assertIn("#define HPWMI_BACKLIGHT 0x20009", self.source)
        self.assertIn("OMEN_RGB_TABLE_SIZE", self.source)
        self.assertIn("omen_rgb_table_valid", self.source)
        self.assertIn("mutex_lock", self.source)
        self.assertNotRegex(self.source, r"copy_from_user|memdup_user")

    def test_every_firmware_query_callsite_is_allowlisted(self):
        query_args = [
            arg
            for arg in re.findall(r"\bhp_query\(\s*([^,\s]+)", self.source)
            if arg != "u32"
        ]
        self.assertEqual(
            set(query_args),
            {
                "HPWMI_GET_KEYBOARD_TYPE_QUERY",
                "HPWMI_COLOR_GET_QUERY",
                "HPWMI_COLOR_SET_QUERY",
                "HPWMI_STATE_GET_QUERY",
            },
        )
        self.assertEqual(len(query_args), 4)


if __name__ == "__main__":
    unittest.main()
