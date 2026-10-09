import io
import json
import socket
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from okeylitctl.cli import main
from okeylitctl.ipc import ConflictError, IPCBackend
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
    def test_default_backend_uses_broker_and_preserves_json_status(self):
        from test_ipc import BrokerFixture

        with TemporaryDirectory() as directory:
            path = Path(directory) / "broker.sock"
            fixture = BrokerFixture(path, b"OK off 111111,222222,333333,444444 AAAAAA,BBBBBB,CCCCCC,DDDDDD\n")
            out, err = io.StringIO(), io.StringIO()
            try:
                with patch("okeylitctl.cli.IPCBackend", side_effect=lambda: IPCBackend(socket_path=path)):
                    code = main(["status", "--json"], stdout=out, stderr=err)
            finally:
                fixture.close()
        self.assertEqual(code, 0)
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(fixture.requests, [b"1 STATUS\n"])
        self.assertEqual(json.loads(out.getvalue()), {
            "state": "off",
            "colors": ["111111", "222222", "333333", "444444"],
            "original": ["AAAAAA", "BBBBBB", "CCCCCC", "DDDDDD"],
        })

    def test_default_backend_sends_mutation_through_broker(self):
        from test_ipc import BrokerFixture

        with TemporaryDirectory() as directory:
            path = Path(directory) / "broker.sock"
            fixture = BrokerFixture(path, b"OK\n")
            out, err = io.StringIO(), io.StringIO()
            try:
                with patch("okeylitctl.cli.IPCBackend", side_effect=lambda: IPCBackend(socket_path=path)):
                    code = main(["colors", "ffffff,000000,123456,abcdef"], stdout=out, stderr=err)
            finally:
                fixture.close()
        self.assertEqual(code, 0)
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(fixture.requests, [b"1 SET FFFFFF,000000,123456,ABCDEF\n"])

    def test_default_tui_receives_unprivileged_backend(self):
        launched = []
        code = main(["tui"], stdout=io.StringIO(), stderr=io.StringIO(),
                    tui_runner=launched.append)
        self.assertEqual(code, 0)
        self.assertEqual(len(launched), 1)
        self.assertIsInstance(launched[0], IPCBackend)

    def test_broker_error_retains_cli_exit_code_without_success_output(self):
        from test_ipc import BrokerFixture

        with TemporaryDirectory() as directory:
            path = Path(directory) / "broker.sock"
            fixture = BrokerFixture(path, b"ERR BUSY\n")
            out, err = io.StringIO(), io.StringIO()
            try:
                with patch("okeylitctl.cli.IPCBackend", side_effect=lambda: IPCBackend(socket_path=path)):
                    code = main(["restore"], stdout=out, stderr=err)
            finally:
                fixture.close()
        self.assertEqual(code, 5)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("busy", err.getvalue().lower())
        self.assertEqual(fixture.requests, [b"1 RESTORE\n"])

    def test_mutation_broker_io_or_abi_error_is_visible_without_success_or_sudo_hint(self):
        from test_ipc import BrokerFixture

        for reply, expected_exit in ((b"ERR IO\n", 5), (b"ERR ABI\n", 6)):
            with self.subTest(reply=reply), TemporaryDirectory() as directory:
                path = Path(directory) / "broker.sock"
                fixture = BrokerFixture(path, reply)
                out, err = io.StringIO(), io.StringIO()
                try:
                    with patch("okeylitctl.cli.IPCBackend", side_effect=lambda: IPCBackend(socket_path=path)):
                        code = main(["restore"], stdout=out, stderr=err)
                finally:
                    fixture.close()
                self.assertEqual(code, expected_exit)
                self.assertEqual(out.getvalue(), "")
                self.assertIn("outcome uncertain", err.getvalue().lower())
                self.assertIn("do not retry", err.getvalue().lower())
                self.assertNotIn("sudo", err.getvalue().lower())
                self.assertEqual(fixture.requests, [b"1 RESTORE\n"])

    def test_permission_failure_never_suggests_sudo(self):
        backend = FakeBackend()
        backend.error = PermissionDenied("broker denied access")
        code, _, err, _ = self.run_cli("restore", backend=backend)
        self.assertEqual(code, 4)
        self.assertNotIn("sudo", err.lower())
        self.assertNotIn("run as root", err.lower())
        self.assertIn("broker", err.lower())

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

    def test_named_set_with_snapshot_remerges_after_conflict(self):
        class RacingBackend(FakeBackend):
            def __init__(self):
                super().__init__()
                self.token = "0000000000000001:0000000000000001"

            def snapshot(self):
                self.calls.append(("snapshot", self.token))
                return {**self.status_value, "token": self.token}

            def compare_and_write(self, token, state, expected_wire, desired_wire):
                self.calls.append(("cas", token, state, expected_wire, desired_wire))
                if len([call for call in self.calls if call[0] == "cas"]) == 1:
                    self.status_value["colors"][1] = "C0FFEE"
                    self.token = "0000000000000002:0000000000000002"
                    raise ConflictError("broker snapshot conflict")
                self.status_value["colors"] = desired_wire.split(",")
                return "0000000000000003:0000000000000003"

        backend = RacingBackend()
        code, out, err, _ = self.run_cli("set", "--right", "abcdef", backend=backend)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(backend.calls, [
            ("snapshot", "0000000000000001:0000000000000001"),
            ("cas", "0000000000000001:0000000000000001", "on",
             "FF0000,00FF00,0000FF,FFFFFF", "ABCDEF,00FF00,0000FF,FFFFFF"),
            ("snapshot", "0000000000000002:0000000000000002"),
            ("cas", "0000000000000002:0000000000000002", "on",
             "FF0000,C0FFEE,0000FF,FFFFFF", "ABCDEF,C0FFEE,0000FF,FFFFFF"),
        ])
        self.assertIn("ABCDEF,C0FFEE,0000FF,FFFFFF", out)

    def test_named_set_uses_real_seqpacket_snapshot_cas_after_disjoint_race(self):
        responses = (
            b"OK 0000000000000001:0000000000000001 on FF0000,00FF00,0000FF,FFFFFF 580BC3,D00FEF,4D0998,AF0AA1\n",
            b"ERR CONFLICT\n",
            b"OK 0000000000000002:0000000000000002 on FF0000,C0FFEE,0000FF,FFFFFF 580BC3,D00FEF,4D0998,AF0AA1\n",
            b"OK 0000000000000003:0000000000000003\n",
        )
        requests = []
        errors = []
        with TemporaryDirectory() as directory:
            path = Path(directory) / "broker.sock"
            with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as listener:
                listener.bind(str(path))
                listener.listen(4)
                listener.settimeout(2)

                def serve():
                    try:
                        for response in responses:
                            with listener.accept()[0] as conn:
                                requests.append(conn.recv(129))
                                conn.sendall(response)
                    except Exception as exc:
                        errors.append(exc)

                thread = threading.Thread(target=serve, daemon=True)
                thread.start()
                out, err = io.StringIO(), io.StringIO()
                with patch("okeylitctl.cli.IPCBackend", side_effect=lambda: IPCBackend(socket_path=path)):
                    code = main(["set", "--right", "abcdef"], stdout=out, stderr=err)
                thread.join(3)
                self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual((code, err.getvalue()), (0, ""))
        self.assertEqual(requests, [
            b"2 SNAPSHOT\n",
            b"2 CAS 0000000000000001:0000000000000001 on FF0000,00FF00,0000FF,FFFFFF ABCDEF,00FF00,0000FF,FFFFFF\n",
            b"2 SNAPSHOT\n",
            b"2 CAS 0000000000000002:0000000000000002 on FF0000,C0FFEE,0000FF,FFFFFF ABCDEF,C0FFEE,0000FF,FFFFFF\n",
        ])
        self.assertIn("ABCDEF,C0FFEE,0000FF,FFFFFF", out.getvalue())

    def test_named_set_conflict_retries_are_bounded_without_success_output(self):
        class AlwaysConflicting(FakeBackend):
            def snapshot(self):
                self.calls.append(("snapshot",))
                return {**self.status_value, "token": "0123456789ABCDEF:FEDCBA9876543210"}

            def compare_and_write(self, *args):
                self.calls.append(("cas", *args))
                raise ConflictError("broker snapshot conflict")

        backend = AlwaysConflicting()
        code, out, err, _ = self.run_cli("set", "--left", "123456", backend=backend)
        self.assertEqual(code, 5)
        self.assertEqual(out, "")
        self.assertIn("conflict", err)
        self.assertEqual([call[0] for call in backend.calls],
                         ["snapshot", "cas"] * 3)

    def test_named_set_never_retries_uncertain_mutation(self):
        from okeylitctl.sysfs import BackendIOError

        class UncertainBackend(FakeBackend):
            def snapshot(self):
                self.calls.append(("snapshot",))
                return {**self.status_value, "token": "0123456789ABCDEF:FEDCBA9876543210"}

            def compare_and_write(self, *args):
                self.calls.append(("cas", *args))
                raise BackendIOError("mutation outcome uncertain; do not retry automatically")

        backend = UncertainBackend()
        code, out, err, _ = self.run_cli("set", "--left", "123456", backend=backend)
        self.assertEqual(code, 5)
        self.assertEqual(out, "")
        self.assertIn("outcome uncertain", err)
        self.assertEqual([call[0] for call in backend.calls], ["snapshot", "cas"])

    def test_all_and_colors_use_full_write_even_with_snapshot_capability(self):
        class AtomicBackend(FakeBackend):
            def snapshot(self):
                raise AssertionError("full writes must not snapshot")

            def compare_and_write(self, *args):
                raise AssertionError("full writes must not CAS")

        for argv, expected in (
            (("set", "--all", "abcdef"), "ABCDEF,ABCDEF,ABCDEF,ABCDEF"),
            (("colors", "111111,222222,333333,444444"), "111111,222222,333333,444444"),
        ):
            with self.subTest(argv=argv):
                backend = AtomicBackend()
                code, _, err, _ = self.run_cli(*argv, backend=backend)
                self.assertEqual((code, err), (0, ""))
                self.assertEqual(backend.calls, [("colors", expected)])

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
