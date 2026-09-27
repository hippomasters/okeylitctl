import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class InstallerSafetyTests(unittest.TestCase):
    def test_installer_has_no_network_or_shell_evaluation(self):
        source = (ROOT / "scripts" / "install-dkms.sh").read_text(encoding="utf-8")
        for token in ("curl ", "wget ", "eval ", "source ", "bash -c"):
            with self.subTest(token=token):
                self.assertNotIn(token, source)

    def test_dkms_uses_a_fixed_root_owned_snapshot(self):
        source = (ROOT / "scripts" / "install-dkms.sh").read_text(encoding="utf-8")
        self.assertIn('DEST="/usr/src/${MODULE}-${DRIVER_VERSION}"', source)
        self.assertIn('chown -R root:root "$DEST"', source)
        self.assertIn('chmod -R go-w "$DEST"', source)
        self.assertIn('if [ -e "$path" ] || [ -L "$path" ]', source)

    def test_installs_the_okeylitctl_public_command(self):
        source = (ROOT / "scripts" / "install-dkms.sh").read_text(encoding="utf-8")
        self.assertIn('CLI_DEST="/usr/local/bin/okeylitctl"', source)
        self.assertIn("-m okeylitctl.cli:entrypoint", source)

    def test_installer_creates_a_root_only_profile_directory(self):
        source = (ROOT / "scripts" / "install-dkms.sh").read_text(encoding="utf-8")
        self.assertIn('PROFILE_DIR="/var/lib/okeylitctl"', source)
        self.assertIn('install -d -o root -g root -m 0700 "$PROFILE_DIR"', source)

    def test_public_and_internal_versions_are_explicitly_separate(self):
        for script in ("install-dkms.sh", "uninstall.sh"):
            source = (ROOT / "scripts" / script).read_text(encoding="utf-8")
            with self.subTest(script=script):
                self.assertIn('APP_VERSION="0.2.0"', source)
                self.assertIn('DRIVER_VERSION="0.1.0"', source)

    def test_installer_refuses_a_stale_legacy_cli(self):
        source = (ROOT / "scripts" / "install-dkms.sh").read_text(encoding="utf-8")
        self.assertIn('LEGACY_CLI_DEST="/usr/local/bin/omen-rgb"', source)
        self.assertIn('"$LEGACY_CLI_DEST"', source)

    def test_cli_is_never_installed_setuid(self):
        source = (ROOT / "scripts" / "install-dkms.sh").read_text(encoding="utf-8")
        self.assertIn("install -m 0755", source)
        self.assertNotIn("4755", source)

    def test_root_environment_is_sanitized_before_commands(self):
        source = (ROOT / "scripts" / "install-dkms.sh").read_text(encoding="utf-8")
        self.assertLess(source.index("PATH=/usr/sbin:/usr/bin:/sbin:/bin"),
                        source.index("$(id -u)"))
        self.assertIn("unset PYTHONPATH PYTHONHOME PYTHONSTARTUP PYTHONINSPECT", source)
        self.assertIn("python3 -I -m zipapp", source)

    def test_failed_install_has_cleanup_trap(self):
        source = (ROOT / "scripts" / "install-dkms.sh").read_text(encoding="utf-8")
        self.assertIn("trap cleanup 0", source)
        self.assertIn('dkms remove -m "$MODULE" -v "$DRIVER_VERSION" --all', source)
        self.assertIn('if [ "$COMPLETE" -ne 1 ]', source)

    def test_cleanup_only_removes_artifacts_created_by_this_run(self):
        source = (ROOT / "scripts" / "install-dkms.sh").read_text(encoding="utf-8")
        for flag in ("CREATED_DEST", "DKMS_ADDED", "INSTALLED_CLI", "INSTALLED_LOAD"):
            with self.subTest(flag=flag):
                self.assertIn(flag, source)
        self.assertIn("refusing existing installed path", source)

    def test_uninstaller_removes_new_and_legacy_cli_names(self):
        source = (ROOT / "scripts" / "uninstall.sh").read_text(encoding="utf-8")
        self.assertIn("/usr/local/bin/okeylitctl", source)
        self.assertIn("/usr/local/bin/omen-rgb", source)

    def test_uninstaller_fails_closed_when_dkms_status_fails(self):
        source = (ROOT / "scripts" / "uninstall.sh").read_text(encoding="utf-8")
        self.assertIn("for command in dkms modprobe", source)
        self.assertNotIn("dkms status -m \"$MODULE\" -v \"$VERSION\" 2>/dev/null || true", source)

    def test_failed_install_unloads_a_module_it_may_have_loaded(self):
        source = (ROOT / "scripts" / "install-dkms.sh").read_text(encoding="utf-8")
        self.assertIn("MAY_HAVE_LOADED", source)
        self.assertIn('modprobe -r omen_rgb', source)


if __name__ == "__main__":
    unittest.main()
