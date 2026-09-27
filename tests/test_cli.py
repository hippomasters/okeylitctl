import io
import json
import unittest

from okeylitctl.cli import main
from okeylitctl.models import ColorLayout
from okeylitctl.sysfs import ModuleUnavailable, PermissionDenied, UnsupportedABI


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


class FakeProfileStore:
    def __init__(self):
        self.calls = []
        self.layouts = {
            "Work": ColorLayout.from_wire("111111,222222,333333,444444")
        }

    def save(self, name, layout, *, overwrite=False):
        self.calls.append(("save", name, layout.to_wire(), overwrite))
        self.layouts[name] = layout

    def load(self, name):
        self.calls.append(("load", name))
        return self.layouts[name]

    def list_names(self):
        self.calls.append(("list",))
        return tuple(sorted(self.layouts))

    def delete(self, name):
        self.calls.append(("delete", name))
        del self.layouts[name]


class CliTests(unittest.TestCase):
    def run_cli(self, *argv, backend=None, profile_store=None):
        out, err = io.StringIO(), io.StringIO()
        backend = backend or FakeBackend()
        code = main(
            list(argv),
            backend=backend,
            profile_store=profile_store,
            stdout=out,
            stderr=err,
        )
        return code, out.getvalue(), err.getvalue(), backend

    def test_profile_save_captures_current_complete_layout(self):
        profiles = FakeProfileStore()

        code, out, err, backend = self.run_cli(
            "profile", "save", "Gaming", profile_store=profiles
        )

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(backend.calls, [])
        self.assertEqual(
            profiles.calls,
            [("save", "Gaming", "FF0000,00FF00,0000FF,FFFFFF", False)],
        )
        self.assertIn("Gaming", out)

    def test_profile_save_validates_name_before_device_access(self):
        backend = FakeBackend()
        backend.status = lambda: self.fail("invalid profile name must not read device")
        profiles = FakeProfileStore()

        code, _, err, backend = self.run_cli(
            "profile", "save", "../escape", backend=backend, profile_store=profiles
        )

        self.assertEqual(code, 7)
        self.assertEqual(backend.calls, [])
        self.assertEqual(profiles.calls, [])
        self.assertIn("profile name", err)

    def test_profile_load_performs_one_complete_layout_write(self):
        profiles = FakeProfileStore()

        code, out, err, backend = self.run_cli(
            "profile", "load", "Work", profile_store=profiles
        )

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(profiles.calls, [("load", "Work")])
        self.assertEqual(
            backend.calls,
            [("colors", "111111,222222,333333,444444")],
        )
        self.assertIn("Work", out)

    def test_profile_list_does_not_access_the_device(self):
        profiles = FakeProfileStore()
        backend = FakeBackend()
        backend.status = lambda: self.fail("profile list must not read the device")

        code, out, err, backend = self.run_cli(
            "profile", "list", backend=backend, profile_store=profiles
        )

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(backend.calls, [])
        self.assertEqual(profiles.calls, [("list",)])
        self.assertEqual(out, "Work\n")

    def test_profile_delete_does_not_access_the_device(self):
        profiles = FakeProfileStore()
        backend = FakeBackend()
        backend.status = lambda: self.fail("profile delete must not read the device")

        code, out, err, backend = self.run_cli(
            "profile", "delete", "Work", backend=backend, profile_store=profiles
        )

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(backend.calls, [])
        self.assertEqual(profiles.calls, [("delete", "Work")])
        self.assertIn("Work", out)

    def test_tui_command_launches_with_the_same_backend(self):
        out, err = io.StringIO(), io.StringIO()
        backend = FakeBackend()
        launched = []

        code = main(
            ["tui"],
            backend=backend,
            stdout=out,
            stderr=err,
            tui_runner=lambda received_backend: launched.append(received_backend),
        )

        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(launched, [backend])

    def test_public_command_name_is_okeylitctl(self):
        code, out, err, _ = self.run_cli("--help")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("usage: okeylitctl", out)

    def test_public_release_version_is_0_2_0(self):
        code, out, err, _ = self.run_cli("--version")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(out, "okeylitctl 0.2.0\n")

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

    def test_named_set_updates_selected_zones_as_one_complete_layout(self):
        code, out, err, backend = self.run_cli(
            "set", "--right", "abcdef", "--wasd", "123456"
        )

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(
            backend.calls,
            [("colors", "ABCDEF,00FF00,0000FF,123456")],
        )
        self.assertIn("ABCDEF,00FF00,0000FF,123456", out)

    def test_named_set_all_creates_a_uniform_complete_layout(self):
        code, _, err, backend = self.run_cli("set", "--all", "a1b2c3")

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(
            backend.calls,
            [("colors", "A1B2C3,A1B2C3,A1B2C3,A1B2C3")],
        )

    def test_named_set_rejects_empty_or_conflicting_requests(self):
        for argv in (("set",), ("set", "--all", "FFFFFF", "--left", "000000")):
            with self.subTest(argv=argv):
                code, _, err, backend = self.run_cli(*argv)
                self.assertEqual(code, 2)
                self.assertEqual(backend.calls, [])
                self.assertIn("set", err.lower())

    def test_named_set_validates_before_reading_or_writing_device(self):
        backend = FakeBackend()
        backend.status = lambda: self.fail("invalid color must not read the device")

        code, _, err, backend = self.run_cli(
            "set", "--center", "not-a-color", backend=backend
        )

        self.assertEqual(code, 2)
        self.assertEqual(backend.calls, [])
        self.assertIn("RRGGBB", err)

    def test_named_set_rejects_repeated_options_before_device_access(self):
        for argv in (
            ("set", "--right", "not-a-color", "--right", "ABCDEF"),
            ("set", "--all", "111111", "--all", "222222"),
        ):
            with self.subTest(argv=argv):
                backend = FakeBackend()
                backend.status = lambda: self.fail(
                    "repeated options must not read the device"
                )

                code, _, err, backend = self.run_cli(*argv, backend=backend)

                self.assertEqual(code, 2)
                self.assertEqual(backend.calls, [])
                self.assertTrue("RRGGBB" in err or "repeated" in err.lower())

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
