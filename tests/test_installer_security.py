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
        self.assertIn('DEST="/usr/src/${MODULE}-${VERSION}"', source)
        self.assertIn('chown -R root:root "$DEST"', source)
        self.assertIn('chmod -R go-w "$DEST"', source)
        self.assertIn('if [ -e "$path" ] || [ -L "$path" ]', source)

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
        self.assertIn('dkms remove -m "$MODULE" -v "$VERSION" --all', source)
        self.assertIn('if [ "$COMPLETE" -ne 1 ]', source)

    def test_cleanup_only_removes_artifacts_created_by_this_run(self):
        source = (ROOT / "scripts" / "install-dkms.sh").read_text(encoding="utf-8")
        for flag in ("CREATED_DEST", "DKMS_ADDED", "INSTALLED_CLI", "INSTALLED_LOAD"):
            with self.subTest(flag=flag):
                self.assertIn(flag, source)
        self.assertIn("refusing existing installed path", source)

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
