"""Device-free reversible deployment of the short diagnostic broker."""
import importlib.util
import pathlib
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/rate-campaign-3-deploy.py"


class CampaignThreeDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="rate-deploy-")
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        spec = importlib.util.spec_from_file_location("rate_deploy", SCRIPT)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.module.BROKER = root / "libexec/broker"
        self.module.SERVICE = root / "systemd/broker.service"
        self.module.MANIFEST = root / "dkms/.okeylitctl-manifest"
        self.production_state = self.module.STATE
        self.module.STATE = root / "state"
        self.module.STATE.mkdir(mode=0o700)
        self.module.BROKER.parent.mkdir()
        self.module.SERVICE.parent.mkdir()
        self.module.BROKER.write_bytes(b"ordinary executable")
        self.module.BROKER.chmod(0o755)
        self.module.SERVICE.write_bytes(b"ordinary service")
        self.module.SERVICE.chmod(0o644)
        self.module.MANIFEST.parent.mkdir()
        self.stage = root / "stage"
        self.stage.mkdir(mode=0o700)
        self.diagnostic = self.stage / "diagnostic"
        self.unit = self.stage / "diagnostic.service"
        self.diagnostic.write_bytes(b"diagnostic executable")
        self.diagnostic.chmod(0o755)
        self.unit.write_bytes(b"diagnostic service")
        self.unit.chmod(0o644)
        self.commands = []
        self.systemctl = mock.patch.object(self.module, "systemctl",
                                           side_effect=lambda *args: self.commands.append(args))
        self.systemctl.start()
        self.addCleanup(self.systemctl.stop)
        self.hash = self.module.digest
        self.module.MANIFEST.write_text("okeylitctl-manifest-v2\n" +
                                        ("0" * 64 + "\n") * 6 +
                                        self.hash(self.module.BROKER) + "\n" +
                                        self.hash(self.module.SERVICE) + "\n" +
                                        ("0" * 64 + "\n") * 2)
        self.module.MANIFEST.chmod(0o600)

    def install(self):
        self.module.deploy(self.diagnostic, self.hash(self.diagnostic), self.unit,
                           self.hash(self.unit), self.hash(self.module.BROKER),
                           self.hash(self.module.SERVICE))

    def test_fresh_campaign_does_not_touch_consumed_legacy_markers(self):
        legacy = self.module.STATE.parent / "legacy-campaign"
        previous = self.module.STATE.parent / "previous-campaign"
        previous.mkdir(mode=0o700)
        legacy.mkdir(mode=0o700)
        markers = {name: b"prior trial" for name in ("attempted", "rearms", "used")}
        for name, data in markers.items():
            (legacy / name).write_bytes(data)
            (previous / name).write_bytes(data)
        self.install()
        self.assertEqual({name: (legacy / name).read_bytes() for name in markers}, markers)
        self.assertEqual({name: (previous / name).read_bytes() for name in markers}, markers)
        self.module.restore()
        self.assertEqual({name: (legacy / name).read_bytes() for name in markers}, markers)
        self.assertEqual({name: (previous / name).read_bytes() for name in markers}, markers)

    def test_campaign_three_paths_are_disjoint_from_legacy(self):
        self.assertEqual(self.production_state.name, "okeylitctl-rate-experiment-3")
        self.assertNotEqual(self.production_state,
                            pathlib.Path("/var/lib/okeylitctl-rate-experiment"))
        self.assertNotEqual(self.production_state,
                            pathlib.Path("/var/lib/okeylitctl-rate-experiment-2"))

    def test_deploy_restores_byte_exact_ordinary_broker_and_unit(self):
        self.install()
        self.assertEqual(self.module.BROKER.read_bytes(), b"diagnostic executable")
        self.assertEqual(self.module.SERVICE.read_bytes(), b"diagnostic service")
        self.assertTrue((self.module.STATE / "receipt").exists())
        self.module.restore()
        self.assertEqual(self.module.BROKER.read_bytes(), b"ordinary executable")
        self.assertEqual(self.module.SERVICE.read_bytes(), b"ordinary service")
        self.assertFalse((self.module.STATE / "receipt").exists())
        self.assertEqual(self.commands.count(("stop", "okeylitctl-broker.socket")), 2)
        self.assertEqual(self.commands.count(("start", "okeylitctl-broker.socket")), 2)

    def test_restart_does_not_rearm_and_only_one_reset_survives_reload(self):
        self.install()
        stamp = self.module.STATE / "used"
        stamp.write_bytes(b"")
        stamp.chmod(0o600)
        self.module.rearm()
        self.assertFalse(stamp.exists())
        self.assertEqual((self.module.STATE / "rearms").read_bytes(), b"1")
        self.assertIn(("stop", "okeylitctl-broker.service"), self.commands)
        stamp.write_bytes(b"")
        stamp.chmod(0o600)
        before = self.commands[:]
        # Re-importing the deployment tool models a fresh process after reboot.
        spec = importlib.util.spec_from_file_location("reloaded_deploy", SCRIPT)
        reloaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(reloaded)
        reloaded.BROKER, reloaded.SERVICE = self.module.BROKER, self.module.SERVICE
        reloaded.MANIFEST, reloaded.STATE = self.module.MANIFEST, self.module.STATE
        with self.assertRaises(ValueError):
            reloaded.rearm()
        self.assertEqual(self.commands, before)
        self.module.restore()

    def test_rearm_failure_burns_lease_and_refuses_retry(self):
        self.install()
        stamp = self.module.STATE / "used"
        stamp.write_bytes(b"")
        stamp.chmod(0o600)
        real = self.module.replace

        def fail_after_count(path, data, mode):
            real(path, data, mode)
            if path == self.module.STATE / "rearms":
                raise OSError("crash after durable count")

        with mock.patch.object(self.module, "replace", side_effect=fail_after_count):
            with self.assertRaises(OSError):
                self.module.rearm()
        before = self.commands[:]
        with self.assertRaises(ValueError):
            self.module.rearm()
        self.assertEqual(self.commands, before)
        self.assertTrue(stamp.exists())

    def test_rearm_socket_start_failure_retains_charge_and_refuses_retry(self):
        self.install()
        stamp = self.module.STATE / "used"
        stamp.write_bytes(b"")
        stamp.chmod(0o600)
        real = self.module.systemctl
        failed = False

        def fail_once(*args):
            nonlocal failed
            if args == ("start", "okeylitctl-broker.socket") and not failed:
                failed = True
                raise RuntimeError("socket startup failed")
            real(*args)

        with mock.patch.object(self.module, "systemctl", side_effect=fail_once):
            with self.assertRaises(RuntimeError):
                self.module.rearm()
        self.assertEqual((self.module.STATE / "rearms").read_bytes(), b"1")
        self.assertFalse(stamp.exists())
        stamp.write_bytes(b"")
        stamp.chmod(0o600)
        before = self.commands[:]
        with self.assertRaises(ValueError):
            self.module.rearm()
        self.assertEqual(self.commands, before)

    def test_partial_stop_failure_restarts_ordinary_socket(self):
        real = self.module.systemctl

        def fail_service_stop(*args):
            if args == ("stop", "okeylitctl-broker.service"):
                raise RuntimeError("service stop failed")
            real(*args)

        with mock.patch.object(self.module, "systemctl", side_effect=fail_service_stop):
            with self.assertRaises(RuntimeError):
                self.install()
        self.assertEqual(self.commands[-1], ("start", "okeylitctl-broker.socket"))
        self.assertTrue((self.module.STATE / "attempted").exists())
        self.assertFalse((self.module.STATE / "receipt").exists())

    def test_every_backup_and_receipt_failure_restarts_ordinary_socket(self):
        for name in ("ordinary-broker", "ordinary-service", "receipt"):
            for after_write in (False, True):
                with self.subTest(name=name, after_write=after_write):
                    real = self.module.replace

                    def fail(path, data, mode):
                        if path == self.module.STATE / name and not after_write:
                            raise OSError("injected backup failure")
                        real(path, data, mode)
                        if path == self.module.STATE / name and after_write:
                            raise OSError("injected post-write failure")

                    with mock.patch.object(self.module, "replace", side_effect=fail):
                        with self.assertRaises(OSError):
                            self.install()
                    self.assertEqual(self.module.BROKER.read_bytes(), b"ordinary executable")
                    self.assertEqual(self.module.SERVICE.read_bytes(), b"ordinary service")
                    self.assertEqual(self.commands[-1], ("start", "okeylitctl-broker.socket"))
                    # A partial transaction cannot be silently overwritten.
                    before = self.commands[:]
                    with self.assertRaises(ValueError):
                        self.install()
                    self.assertEqual(self.commands, before)
                    for entry in ("ordinary-broker", "ordinary-service", "receipt",
                                  "attempted"):
                        (self.module.STATE / entry).unlink(missing_ok=True)

    def test_failed_early_recovery_start_keeps_backups(self):
        real_replace = self.module.replace
        real_systemctl = self.module.systemctl

        def fail_backup(path, data, mode):
            if path == self.module.STATE / "ordinary-service":
                raise OSError("backup failure")
            real_replace(path, data, mode)

        def fail_start(*args):
            if args == ("start", "okeylitctl-broker.socket"):
                raise RuntimeError("socket failed")
            real_systemctl(*args)

        with mock.patch.object(self.module, "replace", side_effect=fail_backup), \
             mock.patch.object(self.module, "systemctl", side_effect=fail_start):
            with self.assertRaises(RuntimeError):
                self.install()
        self.assertTrue((self.module.STATE / "ordinary-broker").exists())

    def test_digest_mismatch_and_untrusted_stage_never_stop_service(self):
        with self.assertRaises(ValueError):
            self.module.deploy(self.diagnostic, "0" * 64, self.unit,
                               self.hash(self.unit), self.hash(self.module.BROKER),
                               self.hash(self.module.SERVICE))
        self.assertEqual(self.commands, [])
        self.diagnostic.chmod(0o777)
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual(self.commands, [])

    def test_restoration_refuses_unrelated_installed_binary(self):
        self.install()
        self.module.BROKER.write_bytes(b"not our diagnostic")
        before = self.commands[:]
        with self.assertRaises(ValueError):
            self.module.restore()
        self.assertEqual(self.commands, before)
        self.assertTrue((self.module.STATE / "receipt").exists())

    def test_installed_manifest_mismatch_never_stops_service(self):
        self.module.MANIFEST.write_text("tampered\n")
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual(self.commands, [])

    def test_failed_start_rolls_back_both_files_and_retains_socket(self):
        real = self.module.systemctl
        failed = False

        def fail_first_start(*args):
            nonlocal failed
            if args == ("start", "okeylitctl-broker.socket") and not failed:
                failed = True
                raise RuntimeError("failed socket startup")
            real(*args)

        with mock.patch.object(self.module, "systemctl", side_effect=fail_first_start):
            with self.assertRaises(RuntimeError):
                self.install()
        self.assertEqual(self.module.BROKER.read_bytes(), b"ordinary executable")
        self.assertEqual(self.module.SERVICE.read_bytes(), b"ordinary service")
        self.assertFalse((self.module.STATE / "receipt").exists())

    def test_untrusted_receipt_and_used_stamp_block_privileged_transition(self):
        self.install()
        receipt = self.module.STATE / "receipt"
        receipt.chmod(0o666)
        with self.assertRaises(ValueError):
            self.module.restore()
        receipt.chmod(0o600)
        stamp = self.module.STATE / "used"
        stamp.symlink_to(self.module.BROKER)
        with self.assertRaises(ValueError):
            self.module.rearm()
        self.assertTrue(stamp.is_symlink())

    def test_atomic_backup_syncs_parent_directory_before_deploy(self):
        real_fsync = self.module.os.fsync
        synced = []

        def record(fd):
            import os
            import stat
            synced.append(stat.S_ISDIR(os.fstat(fd).st_mode))
            real_fsync(fd)

        with mock.patch.object(self.module.os, "fsync", side_effect=record):
            self.install()
        self.assertIn(True, synced)

    def test_cli_refuses_to_execute_unstaged_checkout_script_as_root(self):
        with mock.patch.object(self.module, "restore") as restore, \
             mock.patch.object(self.module.os, "geteuid", return_value=0):
            with self.assertRaises(ValueError):
                self.module.main(["restore"])
        restore.assert_not_called()


if __name__ == "__main__":
    unittest.main()
