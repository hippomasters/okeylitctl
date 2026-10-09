import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path


ROOT = Path(__file__).parents[1]


class InstallerSafetyTests(unittest.TestCase):
    @contextmanager
    def isolated(self):
        # The scripts' fixed system paths are rewritten into a private tree.
        # Only command wrappers in build/ are executable (tmp may be noexec).
        with tempfile.TemporaryDirectory(dir=ROOT / "build") as raw:
            root = Path(raw)
            repo = root / "source"
            for directory in ("scripts", "broker", "module", "src/okeylitctl", "packaging"):
                (repo / directory).mkdir(parents=True)
            for name in ("Makefile", "dkms.conf", "omen-rgb.modules-load.conf"):
                shutil.copyfile(ROOT / name, repo / name)
            shutil.copyfile(ROOT / "scripts/export-profiles.py", repo / "scripts/export-profiles.py")
            for name in ("omen_rgb.c", "omen_rgb_protocol.h"):
                shutil.copyfile(ROOT / "module" / name, repo / "module" / name)
            (repo / "src/okeylitctl/__init__.py").write_text('__version__ = "0.2.0"\n')
            (repo / "src/okeylitctl/cli.py").write_text('APP_NAME = "okeylitctl"\ndef entrypoint(): pass\n')
            (repo / "broker/main.c").write_text("int main(void){return 0;}\n")
            for name in ("okeylitctl-broker.service", "okeylitctl-broker.socket"):
                shutil.copyfile(ROOT / "packaging" / name, repo / "packaging" / name)
            prefix = root / "target"
            commands = root / "commands"
            commands.mkdir()
            for name in ("id", "dkms", "modprobe", "systemctl", "getent", "groupadd", "groupdel", "usermod", "cc"):
                wrapper = commands / name
                wrapper.write_text("#!/usr/bin/env python3\n" + '''import os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
state = pathlib.Path(os.environ["FAKE_STATE"])
with (state / "log").open("a") as f: f.write(name + " " + " ".join(args) + "\\n")
if name == "modprobe" and args == ["-r", "omen_rgb"] and (state / "fail_unload").exists():
    sys.exit(1)
if os.environ.get("FAIL_COMMAND") == name + ":" + (args[0] if args else ""):
    sys.exit(1)
if name == "modprobe" and args == ["omen_rgb"]:
    (state / "target/sys/module/omen_rgb").mkdir(parents=True, exist_ok=True)
if name == "modprobe" and args == ["-r", "omen_rgb"]:
    (state / "target/sys/module/omen_rgb").rmdir()
if name == "id":
    print("0" if args == ["-u"] else "users" if args == ["-nG", "alice"] else "")
elif name == "getent":
    if args == ["group", "okeylitctl"]:
        if not (state / "group").exists(): sys.exit(2)
        print("okeylitctl:x:123:alice,bob")
    elif args == ["passwd", "alice"]: print("alice:x:1000:1000::/home/alice:/bin/sh")
    else: sys.exit(2)
elif name == "groupadd": (state / "group").touch()
elif name == "groupdel": (state / "group").unlink()
elif name == "cc":
    out = pathlib.Path(args[args.index("-o") + 1])
    out.write_text("broker\\n")
''')
                wrapper.chmod(0o755)
            substitutions = {
                "/usr/src": str(prefix / "usr/src"),
                "/usr/local/bin": str(prefix / "usr/local/bin"),
                "/usr/local/libexec": str(prefix / "usr/local/libexec"),
                "/etc/modules-load.d": str(prefix / "etc/modules-load.d"),
                "/etc/systemd/system": str(prefix / "etc/systemd/system"),
                "/var/lib/okeylitctl": str(prefix / "var/lib/okeylitctl"),
                "/run/okeylitctl.sock": str(prefix / "run/okeylitctl.sock"),
                "/sys/module/omen_rgb": str(prefix / "sys/module/omen_rgb"),
            }
            for name in ("install-dkms.sh", "uninstall.sh"):
                content = (ROOT / "scripts" / name).read_text()
                for before, after in substitutions.items():
                    content = content.replace(before, after)
                content = content.replace("PATH=/usr/sbin:/usr/bin:/sbin:/bin", "PATH=" + str(commands) + ":/usr/sbin:/usr/bin:/sbin:/bin")
                path = repo / "scripts" / name
                path.write_text(content)
                path.chmod(0o755)
            for directory in (prefix / "usr/local/bin", prefix / "etc/modules-load.d", prefix / "etc/systemd/system", prefix / "usr/src", prefix / "var/lib"):
                directory.mkdir(parents=True)
            env = dict(os.environ, FAKE_STATE=str(root), SUDO_USER="alice", PATH=str(commands) + ":/usr/bin:/bin")
            def run(script, *args, fail=""):
                return subprocess.run(["/bin/sh", str(repo / "scripts" / script), *args],
                                      env={**env, "FAIL_COMMAND": fail}, capture_output=True, text=True)
            yield root, repo, prefix, run

    def test_legacy_020_uninstall_without_broker_socket_and_reinstall(self):
        with self.isolated() as (root, repo, prefix, run):
            dest = prefix / "usr/src/omen-rgb-0.1.0"
            (dest / "module").mkdir(parents=True)
            # Model root-installed DKMS directories, regardless of runner umask.
            dest.chmod(0o755)
            (dest / "module").chmod(0o755)
            for name in ("Makefile", "dkms.conf", "module/omen_rgb.c", "module/omen_rgb_protocol.h"):
                snapshot = dest / name
                snapshot.write_bytes(subprocess.check_output(
                    ["git", "show", f"24296dc:{name}"], cwd=ROOT))
                snapshot.chmod(0o644)
            self.assertNotEqual((dest / "Makefile").read_bytes(), (repo / "Makefile").read_bytes())
            load_entry = prefix / "etc/modules-load.d/omen-rgb.conf"
            load_entry.write_text("omen_rgb\n")
            load_entry.chmod(0o644)
            legacy_src = root / "legacy-src"
            names = subprocess.check_output(
                ["git", "ls-tree", "-r", "--name-only", "24296dc", "src"], cwd=ROOT,
                text=True).splitlines()
            for name in names:
                path = legacy_src / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(subprocess.check_output(["git", "show", f"24296dc:{name}"], cwd=ROOT))
            subprocess.run(["python3", "-I", "-m", "zipapp", str(legacy_src / "src"),
                            "-m", "okeylitctl.cli:entrypoint", "-p", "/usr/bin/python3",
                            "-o", str(prefix / "usr/local/bin/okeylitctl")], check=True)
            (prefix / "usr/local/bin/okeylitctl").chmod(0o755)
            profiles = prefix / "var/lib/okeylitctl/profiles.json"
            profiles.parent.mkdir(mode=0o700)
            profiles.write_text("legacy private data\n")
            result = run("uninstall.sh")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(dest.exists())
            self.assertFalse((prefix / "usr/local/bin/okeylitctl").exists())
            self.assertEqual(profiles.read_text(), "legacy private data\n")
            self.assertNotIn("systemctl disable", (root / "log").read_text())
            result = run("install-dkms.sh")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(profiles.read_text(), "legacy private data\n")

    def test_uninstall_refuses_same_mode_unrelated_files_even_when_other_artifacts_exist(self):
        with self.isolated() as (root, repo, prefix, run):
            self.assertEqual(run("install-dkms.sh").returncode, 0)
            broker = prefix / "usr/local/libexec/okeylitctl-broker"
            broker.write_text("unrelated binary\n")
            before = (root / "log").read_text()
            result = run("uninstall.sh")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(broker.read_text(), "unrelated binary\n")
            self.assertEqual((root / "log").read_text(), before + "id -u\n")

    def test_migration_helper_is_manifested_and_removed_with_install(self):
        with self.isolated() as (root, repo, prefix, run):
            helper = prefix / "usr/local/libexec/okeylitctl-export-profiles"
            self.assertEqual(run("install-dkms.sh").returncode, 0)
            self.assertTrue(helper.is_file())
            self.assertEqual(helper.stat().st_mode & 0o777, 0o755)
            helper.write_text("unrelated script\n")
            self.assertNotEqual(run("uninstall.sh").returncode, 0)
            self.assertEqual(helper.read_text(), "unrelated script\n")
            shutil.copyfile(repo / "scripts/export-profiles.py", helper)
            self.assertEqual(run("uninstall.sh").returncode, 0)
            self.assertFalse(helper.exists())

    def test_installer_requires_migration_source_before_mutation(self):
        with self.isolated() as (root, repo, prefix, run):
            (repo / "scripts/export-profiles.py").unlink()
            self.assertNotEqual(run("install-dkms.sh").returncode, 0)
            self.assertFalse((root / "group").exists())
            self.assertFalse((prefix / "usr/src/omen-rgb-0.1.0").exists())

    def test_legacy_install_rejects_unmanifested_migration_helper(self):
        with self.isolated() as (root, repo, prefix, run):
            helper = prefix / "usr/local/libexec/okeylitctl-export-profiles"
            helper.parent.mkdir(parents=True)
            helper.write_text("unrelated\n")
            self.assertNotEqual(run("uninstall.sh").returncode, 0)
            self.assertEqual(helper.read_text(), "unrelated\n")

    def test_existing_group_requires_explicit_acceptance_even_with_members(self):
        with self.isolated() as (root, repo, prefix, run):
            (root / "group").touch()
            result = run("install-dkms.sh")
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((prefix / "usr/local/bin/okeylitctl").exists())
            self.assertEqual(run("install-dkms.sh", "--accept-existing-group").returncode, 0)

    def test_installer_rejects_symlinked_source_before_any_mutation(self):
        with self.isolated() as (root, repo, prefix, run):
            (repo / "packaging/okeylitctl-broker.socket").unlink()
            (repo / "packaging/okeylitctl-broker.socket").symlink_to(repo / "dkms.conf")
            result = run("install-dkms.sh")
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root / "group").exists())
            self.assertFalse((prefix / "usr/src/omen-rgb-0.1.0").exists())

    def test_uninstall_refuses_unowned_file_without_snapshot(self):
        with self.isolated() as (root, repo, prefix, run):
            file = prefix / "etc/modules-load.d/omen-rgb.conf"
            file.write_text("omen_rgb\n")
            result = run("uninstall.sh")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(file.read_text(), "omen_rgb\n")

    def test_uninstall_refuses_unexpected_snapshot_content(self):
        with self.isolated() as (root, repo, prefix, run):
            self.assertEqual(run("install-dkms.sh").returncode, 0)
            stray = prefix / "usr/src/omen-rgb-0.1.0/other"
            stray.write_text("someone else's content")
            result = run("uninstall.sh")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(stray.read_text(), "someone else's content")

    def test_uninstall_refuses_double_dot_snapshot_entries_without_mutation(self):
        for relative in ("..unrelated", "module/..unrelated"):
            with self.subTest(relative=relative), self.isolated() as (root, repo, prefix, run):
                self.assertEqual(run("install-dkms.sh").returncode, 0)
                dest = prefix / "usr/src/omen-rgb-0.1.0"
                stray = dest / relative
                stray.write_text("foreign data")
                before = (root / "log").read_text()
                result = run("uninstall.sh")
                self.assertNotEqual(result.returncode, 0, relative)
                self.assertEqual(stray.read_text(), "foreign data")
                self.assertTrue((dest / ".okeylitctl-manifest").exists())
                self.assertTrue((prefix / "usr/local/bin/okeylitctl").exists())
                self.assertEqual((root / "log").read_text(), before + "id -u\n")

    def test_failed_module_load_rolls_back_when_module_is_absent_despite_unload_error(self):
        with self.isolated() as (root, repo, prefix, run):
            (root / "fail_unload").touch()
            profile = prefix / "var/lib/okeylitctl"
            profile.mkdir(mode=0o700)
            existing_data = profile / "profiles.json"
            existing_data.write_text("keep existing profiles")
            broker_dir = prefix / "usr/local/libexec"
            broker_dir.mkdir(parents=True)
            broker_dir.chmod(0o755)
            existing_file = broker_dir / "other-service"
            existing_file.write_text("keep existing service")

            result = run("install-dkms.sh", fail="modprobe:omen_rgb")
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((prefix / "sys/module/omen_rgb").exists())
            log = (root / "log").read_text()
            self.assertIn("modprobe omen_rgb\n", log)
            self.assertIn("modprobe -r omen_rgb\n", log)
            self.assertIn("dkms remove -m omen-rgb -v 0.1.0 --all\n", log)
            self.assertIn("groupdel okeylitctl\n", log)
            self.assertNotIn("keeping DKMS", result.stderr)
            for artifact in (
                "usr/src/omen-rgb-0.1.0",
                "usr/local/bin/okeylitctl",
                "usr/local/libexec/okeylitctl-broker",
                "usr/local/libexec/okeylitctl-export-profiles",
                "etc/modules-load.d/omen-rgb.conf",
                "etc/systemd/system/okeylitctl-broker.service",
                "etc/systemd/system/okeylitctl-broker.socket",
            ):
                with self.subTest(artifact=artifact):
                    self.assertFalse((prefix / artifact).exists())
            self.assertFalse((root / "group").exists())
            self.assertEqual(existing_data.read_text(), "keep existing profiles")
            self.assertEqual(existing_file.read_text(), "keep existing service")

    def test_failed_install_keeps_recovery_artifacts_when_module_cannot_unload(self):
        with self.isolated() as (root, repo, prefix, run):
            (root / "fail_unload").touch()
            result = run("install-dkms.sh", fail="systemctl:enable")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unload", result.stderr.lower())
            self.assertTrue((prefix / "sys/module/omen_rgb").is_dir())
            self.assertTrue((prefix / "usr/src/omen-rgb-0.1.0").is_dir())
            self.assertTrue((prefix / "usr/local/bin/okeylitctl").is_file())
            self.assertTrue((prefix / "usr/local/libexec/okeylitctl-broker").is_file())
            self.assertTrue((prefix / "etc/modules-load.d/omen-rgb.conf").is_file())
            self.assertTrue((root / "group").exists())
            self.assertNotIn("dkms remove", (root / "log").read_text())

    def test_installer_rejects_symlink_in_cli_tree_and_broker_source(self):
        for link in ("src/okeylitctl/foreign.py", "broker/main.c", "scripts/export-profiles.py"):
            with self.subTest(link=link), self.isolated() as (root, repo, prefix, run):
                source = repo / link
                if source.exists():
                    source.unlink()
                source.symlink_to(repo / "dkms.conf")
                result = run("install-dkms.sh")
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((root / "group").exists())

    def test_reinstall_after_uninstall_requires_explicit_existing_group_acceptance(self):
        with self.isolated() as (root, repo, prefix, run):
            self.assertEqual(run("install-dkms.sh").returncode, 0)
            self.assertEqual(run("uninstall.sh").returncode, 0)
            self.assertNotEqual(run("install-dkms.sh").returncode, 0)
            self.assertEqual(run("install-dkms.sh", "--accept-existing-group").returncode, 0)

    def test_static_broker_contract(self):
        install = (ROOT / "scripts/install-dkms.sh").read_text()
        uninstall = (ROOT / "scripts/uninstall.sh").read_text()
        socket = (ROOT / "packaging/okeylitctl-broker.socket").read_text()
        service = (ROOT / "packaging/okeylitctl-broker.service").read_text()
        self.assertIn("ListenSequentialPacket=/run/okeylitctl.sock", socket)
        self.assertIn("SocketGroup=okeylitctl", socket)
        self.assertIn("SocketMode=0660", socket)
        self.assertIn("ExecStart=/usr/local/libexec/okeylitctl-broker", service)
        self.assertIn("User=root", service)
        self.assertIn("NoNewPrivileges=yes", service)
        self.assertNotIn("ProtectKernelTunables=yes", service)
        self.assertNotIn("AmbientCapabilities=", service)
        self.assertIn('BROKER_SOURCE="$SOURCE/broker/main.c"', install)
        self.assertIn('cc ', install)
        self.assertIn('case "$1" in', install)
        self.assertNotIn("SUDO_USER", install + uninstall)
        self.assertNotIn("4755", install)
        self.assertNotIn("sudo ", install + uninstall)
        for token in ("curl ", "wget ", "eval ", "bash -c"):
            self.assertNotIn(token, install)
        self.assertIn('DEST="/usr/src/${MODULE}-${DRIVER_VERSION}"', install)
        self.assertIn('PROFILE_DIR="/var/lib/okeylitctl"', install)
        self.assertIn('DRIVER_VERSION="0.1.0"', install + uninstall)
        self.assertIn('"$BROKER_DEST"', uninstall)
        self.assertNotIn('rm -rf -- "$PROFILE_DIR"', uninstall)

    def test_install_rollback_and_explicit_enrollment_in_isolated_filesystem(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "build") as raw:
            root = Path(raw)
            repo = root / "source"
            (repo / "scripts").mkdir(parents=True)
            (repo / "broker").mkdir()
            (repo / "module").mkdir()
            (repo / "src/okeylitctl").mkdir(parents=True)
            (repo / "packaging").mkdir()
            for name in ("Makefile", "dkms.conf", "omen-rgb.modules-load.conf"):
                shutil.copyfile(ROOT / name, repo / name)
            shutil.copyfile(ROOT / "scripts/export-profiles.py", repo / "scripts/export-profiles.py")
            for name in ("omen_rgb.c", "omen_rgb_protocol.h"):
                shutil.copyfile(ROOT / "module" / name, repo / "module" / name)
            (repo / "src/okeylitctl/__init__.py").write_text("")
            (repo / "src/okeylitctl/cli.py").write_text("def entrypoint(): pass\n")
            (repo / "broker/main.c").write_text("int main(void){return 0;}\n")
            for name in ("okeylitctl-broker.service", "okeylitctl-broker.socket"):
                shutil.copyfile(ROOT / "packaging" / name, repo / "packaging" / name)
            prefix = root / "target"
            commands = root / "commands"
            commands.mkdir()
            for name in ("id", "dkms", "modprobe", "systemctl", "getent", "groupadd", "groupdel", "usermod", "cc"):
                wrapper = commands / name
                wrapper.write_text("#!/usr/bin/env python3\n" + '''import os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
state = pathlib.Path(os.environ["FAKE_STATE"])
with (state / "log").open("a") as f: f.write(name + " " + " ".join(args) + "\\n")
if name == "modprobe" and args == ["-r", "omen_rgb"] and (state / "fail_unload").exists():
    sys.exit(1)
if os.environ.get("FAIL_COMMAND") == name + ":" + (args[0] if args else ""):
    sys.exit(1)
if name == "modprobe" and args == ["omen_rgb"]:
    (state / "target/sys/module/omen_rgb").mkdir(parents=True, exist_ok=True)
if name == "modprobe" and args == ["-r", "omen_rgb"]:
    (state / "target/sys/module/omen_rgb").rmdir()
if name == "id":
    print("0" if args == ["-u"] else "users" if args == ["-nG", "alice"] else "")
elif name == "getent":
    if args == ["group", "okeylitctl"]:
        if not (state / "group").exists(): sys.exit(2)
        print("okeylitctl:x:123:")
    elif args == ["passwd", "alice"]: print("alice:x:1000:1000::/home/alice:/bin/sh")
    else: sys.exit(2)
elif name == "groupadd": (state / "group").touch()
elif name == "groupdel": (state / "group").unlink()
elif name == "cc":
    out = pathlib.Path(args[args.index("-o") + 1])
    out.write_text("broker\\n")
else: pass
''')
                wrapper.chmod(0o755)
            substitutions = {
                "/usr/src": str(prefix / "usr/src"),
                "/usr/local/bin": str(prefix / "usr/local/bin"),
                "/usr/local/libexec": str(prefix / "usr/local/libexec"),
                "/etc/modules-load.d": str(prefix / "etc/modules-load.d"),
                "/etc/systemd/system": str(prefix / "etc/systemd/system"),
                "/var/lib/okeylitctl": str(prefix / "var/lib/okeylitctl"),
                "/run/okeylitctl.sock": str(prefix / "run/okeylitctl.sock"),
                "/sys/module/omen_rgb": str(prefix / "sys/module/omen_rgb"),
            }
            for name in ("install-dkms.sh", "uninstall.sh"):
                content = (ROOT / "scripts" / name).read_text()
                for before, after in substitutions.items():
                    content = content.replace(before, after)
                content = content.replace("PATH=/usr/sbin:/usr/bin:/sbin:/bin", "PATH=" + str(commands) + ":/usr/sbin:/usr/bin:/sbin:/bin")
                path = repo / "scripts" / name
                path.write_text(content)
                path.chmod(0o755)
            for directory in (prefix / "usr/local/bin", prefix / "etc/modules-load.d", prefix / "etc/systemd/system", prefix / "usr/src", prefix / "var/lib"):
                directory.mkdir(parents=True)
            env = dict(os.environ, FAKE_STATE=str(root), SUDO_USER="alice", PATH=str(commands) + ":/usr/bin:/bin")
            def run(script, *args, fail=""):
                return subprocess.run(["/bin/sh", str(repo / "scripts" / script), *args],
                                      env={**env, "FAIL_COMMAND": fail}, capture_output=True, text=True)
            cli = prefix / "usr/local/bin/okeylitctl"
            broker = prefix / "usr/local/libexec/okeylitctl-broker"
            helper = prefix / "usr/local/libexec/okeylitctl-export-profiles"
            sockunit = prefix / "etc/systemd/system/okeylitctl-broker.socket"
            profile = prefix / "var/lib/okeylitctl"
            protected = root / "protected"
            protected.write_text("do not touch")
            cli.symlink_to(protected)
            self.assertNotEqual(run("install-dkms.sh").returncode, 0)
            self.assertEqual(protected.read_text(), "do not touch")
            self.assertFalse((root / "group").exists())
            cli.unlink()
            profile.mkdir(mode=0o777)
            profile.chmod(0o777)
            self.assertNotEqual(run("install-dkms.sh").returncode, 0)
            self.assertEqual(profile.stat().st_mode & 0o777, 0o777)
            profile.rmdir()
            for failure in ("dkms:add", "cc:-std=c11", "systemctl:enable"):
                result = run("install-dkms.sh", fail=failure)
                self.assertNotEqual(result.returncode, 0, (failure, result.stderr))
                self.assertFalse(cli.exists())
                self.assertFalse(broker.exists())
                self.assertFalse(helper.exists())
                self.assertFalse((prefix / "usr/local/libexec").exists())
                self.assertFalse(sockunit.exists())
                self.assertFalse((root / "group").exists())
                self.assertFalse(profile.exists())
                self.assertFalse((prefix / "usr/src/omen-rgb-0.1.0").exists())
            result = run("install-dkms.sh")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(broker.stat().st_mode & 0o7777, 0o755)
            self.assertEqual(helper.stat().st_mode & 0o7777, 0o755)
            self.assertEqual(cli.stat().st_mode & 0o7777, 0o755)
            self.assertEqual(profile.stat().st_mode & 0o7777, 0o700)
            self.assertTrue(sockunit.exists())
            self.assertNotIn("usermod ", (root / "log").read_text())
            self.assertNotEqual(run("install-dkms.sh").returncode, 0)
            self.assertNotEqual(run("install-dkms.sh", "--allow-user", "../alice").returncode, 0)
            self.assertEqual(run("install-dkms.sh", "--allow-user", "alice").returncode, 0)
            self.assertIn("usermod -a -G okeylitctl alice", (root / "log").read_text())
            data = profile / "profiles.json"
            data.write_text("owned data")
            sockunit.chmod(0o666)
            before = (root / "log").read_text()
            self.assertNotEqual(run("uninstall.sh").returncode, 0)
            self.assertEqual((root / "log").read_text().count("systemctl disable --now"), before.count("systemctl disable --now"))
            self.assertTrue(broker.exists())
            sockunit.chmod(0o644)
            result = run("uninstall.sh")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(broker.exists())
            self.assertFalse(helper.exists())
            self.assertFalse(sockunit.exists())
            self.assertTrue(data.exists())
            self.assertTrue((root / "group").exists())
            # An existing group and an existing profile directory are never rolled back.
            result = run("install-dkms.sh", fail="cc:-std=c11")
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue((root / "group").exists())
            self.assertEqual(data.read_text(), "owned data")


if __name__ == "__main__":
    unittest.main()
