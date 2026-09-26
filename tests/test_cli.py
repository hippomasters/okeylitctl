import io
import json
import unittest

from omen_rgb.cli import main
from omen_rgb.sysfs import ModuleUnavailable, PermissionDenied, UnsupportedABI


class FakeBackend:
    def __init__(self):
        self.calls = []
        self.status_value = {
            "state": "on",
            "colors": ["FF0000", "00FF00", "0000FF", "FFFFFF"],
            "original": ["580BC3", "D00FEF", "4D0998", "AF0AA1"],
        }
        self.error = None

    def _raise(self):
        if self.error:
            raise self.error

    def status(self):
        self._raise()
        return self.status_value

    def write_colors(self, value):
        self._raise()
        self.calls.append(("colors", value))

    def restore(self):
        self._raise()
        self.calls.append(("restore", None))


class CliTests(unittest.TestCase):
    def run_cli(self, *argv, backend=None):
        out, err = io.StringIO(), io.StringIO()
        backend = backend or FakeBackend()
        code = main(list(argv), backend=backend, stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue(), backend

    def test_human_status(self):
        code, out, err, _ = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("State:    on\n", out)
        self.assertIn("Colors:   FF0000,00FF00,0000FF,FFFFFF\n", out)

    def test_json_status_has_stable_schema(self):
        code, out, err, _ = self.run_cli("status", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(
            json.loads(out),
            {
                "state": "on",
                "colors": ["FF0000", "00FF00", "0000FF", "FFFFFF"],
                "original": ["580BC3", "D00FEF", "4D0998", "AF0AA1"],
            },
        )

    def test_colors_are_validated_before_backend_call(self):
        code, _, err, backend = self.run_cli("colors", "not-a-color")
        self.assertEqual(code, 2)
        self.assertEqual(backend.calls, [])
        self.assertIn("four comma-separated", err)

    def test_colors_are_normalized_and_written(self):
        code, out, err, backend = self.run_cli(
            "colors", "ff0000,00ff00,0000ff,ffffff"
        )
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(
            backend.calls,
            [("colors", "FF0000,00FF00,0000FF,FFFFFF")],
        )
        self.assertIn("updated", out.lower())

    def test_state_mutation_command_is_not_exposed(self):
        code, _, err, backend = self.run_cli("state", "off")
        self.assertEqual(code, 2)
        self.assertEqual(backend.calls, [])
        self.assertIn("invalid choice", err)

    def test_restore_calls_only_restore(self):
        code, _, err, backend = self.run_cli("restore")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(backend.calls, [("restore", None)])

    def test_expected_backend_errors_have_stable_exit_codes_without_tracebacks(self):
        cases = [
            (ModuleUnavailable("missing"), 3),
            (PermissionDenied("denied"), 4),
            (UnsupportedABI("bad ABI"), 6),
        ]
        for error, expected in cases:
            with self.subTest(error=error):
                backend = FakeBackend()
                backend.error = error
                code, out, err, _ = self.run_cli("status", backend=backend)
                self.assertEqual(code, expected)
                self.assertEqual(out, "")
                self.assertNotIn("Traceback", err)

    def test_unknown_command_is_usage_error(self):
        code, _, err, _ = self.run_cli("unknown")
        self.assertEqual(code, 2)
        self.assertIn("usage:", err.lower())


if __name__ == "__main__":
    unittest.main()
