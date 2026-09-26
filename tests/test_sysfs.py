import errno
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from omen_rgb.sysfs import (
    BackendIOError,
    ModuleUnavailable,
    PermissionDenied,
    SysfsBackend,
    UnsupportedABI,
)


class SysfsBackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "colors").write_text(
            "580bc3,d00fef,4d0998,af0aa1\n", encoding="ascii"
        )
        (self.root / "original").write_text(
            "580bc3,d00fef,4d0998,af0aa1\n", encoding="ascii"
        )
        (self.root / "state").write_text("on\n", encoding="ascii")
        (self.root / "restore").write_text("0\n", encoding="ascii")
        (self.root / "abi_version").write_text("1\n", encoding="ascii")
        self.backend = SysfsBackend(_root=self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_reads_and_validates_status(self):
        self.assertEqual(
            self.backend.status(),
            {
                "state": "on",
                "colors": ["580BC3", "D00FEF", "4D0998", "AF0AA1"],
                "original": ["580BC3", "D00FEF", "4D0998", "AF0AA1"],
            },
        )

    def test_has_no_state_mutation_method(self):
        self.assertFalse(hasattr(self.backend, "write_state"))

    def test_private_write_rejects_read_only_state(self):
        with self.assertRaises(BackendIOError):
            self.backend._write("state", "off")
        self.assertEqual((self.root / "state").read_text(encoding="ascii"), "on\n")

    def test_writes_only_allowlisted_parameter_with_one_write(self):
        with mock.patch("omen_rgb.sysfs.os.write", wraps=os.write) as write:
            self.backend.write_colors("FF0000,00FF00,0000FF,FFFFFF")
        write.assert_called_once()
        self.assertEqual(
            (self.root / "colors").read_text(encoding="ascii"),
            "FF0000,00FF00,0000FF,FFFFFF\n",
        )

    def test_rejects_short_write(self):
        with mock.patch("omen_rgb.sysfs.os.write", return_value=1):
            with self.assertRaises(BackendIOError):
                self.backend.write_colors("FF0000,00FF00,0000FF,FFFFFF")

    def test_missing_module_maps_to_unavailable(self):
        (self.root / "colors").unlink()
        with self.assertRaises(ModuleUnavailable):
            self.backend.status()

    def test_permission_error_is_distinct(self):
        with mock.patch("omen_rgb.sysfs.os.open", side_effect=PermissionError(errno.EACCES, "no")):
            with self.assertRaises(PermissionDenied):
                self.backend.write_colors("FF0000,00FF00,0000FF,FFFFFF")

    def test_symlink_parameter_is_not_followed(self):
        target = self.root / "target"
        target.write_text("unchanged\n", encoding="ascii")
        (self.root / "colors").unlink()
        (self.root / "colors").symlink_to(target)
        with self.assertRaises(BackendIOError):
            self.backend.write_colors("FF0000,00FF00,0000FF,FFFFFF")
        self.assertEqual(target.read_text(encoding="ascii"), "unchanged\n")

    def test_malformed_kernel_output_fails_closed(self):
        (self.root / "state").write_text("enabled\n", encoding="ascii")
        with self.assertRaises(UnsupportedABI):
            self.backend.status()

    def test_unknown_abi_version_blocks_access(self):
        (self.root / "abi_version").write_text("2\n", encoding="ascii")
        with self.assertRaises(UnsupportedABI):
            self.backend.status()
        with self.assertRaises(UnsupportedABI):
            self.backend.write_colors("FF0000,00FF00,0000FF,FFFFFF")
        with self.assertRaises(UnsupportedABI):
            self.backend.restore()


if __name__ == "__main__":
    unittest.main()
