import tempfile
import unittest
import os
import threading
import subprocess
import sys
import shutil
import zipapp
import zipfile
from pathlib import Path
from unittest import mock

from okeylitctl.models import ColorLayout
from okeylitctl.profiles import ProfileError, ProfileExists, ProfileNotFound, ProfileStore, export_legacy_profiles, import_profiles


class ProfileStoreTests(unittest.TestCase):
    @unittest.skipUnless(os.geteuid() == 0, "root-only helper trust boundary")
    def test_elevated_migration_rejects_checkout_before_package_import(self):
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir="/home") as directory:
            checkout = Path(directory)
            (checkout / "scripts").mkdir()
            (checkout / "src/okeylitctl").mkdir(parents=True)
            script = checkout / "scripts/export-profiles.py"
            shutil.copy2(repo / "scripts/export-profiles.py", script)
            marker = checkout / "executed"
            (checkout / "src/okeylitctl/__init__.py").write_text(
                f"open({str(marker)!r}, 'w').close()\n")
            result = subprocess.run([sys.executable, "-I", str(script), "--export"],
                                    capture_output=True, timeout=5, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"installed helper", result.stderr)
            self.assertFalse(marker.exists())

    @unittest.skipUnless(os.geteuid() == 0, "root-only helper trust boundary")
    def test_elevated_installed_helper_uses_archive_not_checkout_or_pythonpath(self):
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir="/home") as directory:
            root = Path(directory)
            helper = root / "okeylitctl-export-profiles"
            archive = root / "okeylitctl.pyz"
            source = (repo / "scripts/export-profiles.py").read_text()
            source = source.replace("/usr/local/libexec/okeylitctl-export-profiles", str(helper))
            source = source.replace("/usr/local/bin/okeylitctl", str(archive))
            helper.write_text(source)
            helper.chmod(0o755)
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr("okeylitctl/__init__.py", "")
                package.writestr("okeylitctl/profiles.py", '''
class ProfileError(Exception): pass
_MAX_FILE_SIZE = 65536
def _decode_profiles(payload): raise ProfileError("invalid payload")
def export_legacy_profiles(): return b'{"schema":1,"profiles":{}}\\n'
def import_profiles(payload): pass
''')
            hostile = root / "hostile/okeylitctl"
            hostile.mkdir(parents=True)
            marker = root / "executed"
            (hostile / "__init__.py").write_text(f"open({str(marker)!r}, 'w').close()\n")
            env = {**os.environ, "PYTHONPATH": str(hostile.parent)}
            result = subprocess.run([sys.executable, "-I", str(helper), "--export"],
                                    env=env, capture_output=True, timeout=5, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, b'{"schema":1,"profiles":{}}\n')
            self.assertFalse(marker.exists())

    @unittest.skipUnless(os.geteuid() == 0, "root-only helper trust boundary")
    def test_elevated_helper_rejects_symlink_or_writable_archive_ancestor(self):
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir="/home") as directory:
            root = Path(directory)
            helper = root / "okeylitctl-export-profiles"
            archive = root / "untrusted/okeylitctl.pyz"
            archive.parent.mkdir()
            archive.write_bytes(b"not executed")
            source = (repo / "scripts/export-profiles.py").read_text()
            source = source.replace("/usr/local/libexec/okeylitctl-export-profiles", str(helper))
            source = source.replace("/usr/local/bin/okeylitctl", str(archive))
            helper.write_text(source)
            helper.chmod(0o755)
            root.chmod(0o777)
            result = subprocess.run([sys.executable, "-I", str(helper), "--export"],
                                    capture_output=True, timeout=5, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"unsafe", result.stderr)
            root.chmod(0o700)
            archive.parent.chmod(0o777)
            result = subprocess.run([sys.executable, "-I", str(helper), "--export"],
                                    capture_output=True, timeout=5, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"unsafe", result.stderr)
            archive.parent.chmod(0o755)
            archive.unlink()
            archive.symlink_to(helper)
            result = subprocess.run([sys.executable, "-I", str(helper), "--export"],
                                    capture_output=True, timeout=5, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"symlink", result.stderr)

    def test_private_fifo_store_is_rejected_without_waiting_for_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            os.mkfifo(path, 0o600)
            env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
            result = subprocess.run(
                [sys.executable, "-I", "-c",
                 "import sys; sys.path.insert(0, sys.argv[1]); "
                 "from okeylitctl.profiles import ProfileStore, ProfileError; "
                 "\ntry: ProfileStore(sys.argv[2]).list_names()"
                 "\nexcept ProfileError as exc: print(exc)"
                 "\nelse: raise AssertionError('FIFO accepted')",
                 str(Path(__file__).resolve().parents[1] / "src"), str(path)],
                env=env, capture_output=True, timeout=3, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(b"not a regular file", result.stdout)

    def test_private_fifo_lock_is_rejected_without_waiting_for_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / ".profiles.json.lock"
            os.mkfifo(lock, 0o600)
            env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
            result = subprocess.run(
                [sys.executable, "-I", "-c",
                 "import sys; sys.path.insert(0, sys.argv[1]); "
                 "from okeylitctl.profiles import ProfileStore, ProfileError; "
                 "\ntry: ProfileStore(sys.argv[2]).list_names()"
                 "\nexcept ProfileError as exc: print(exc)"
                 "\nelse: raise AssertionError('FIFO lock accepted')",
                 str(Path(__file__).resolve().parents[1] / "src"),
                 str(Path(directory) / "profiles.json")],
                env=env, capture_output=True, timeout=3, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(b"lock is not a regular file", result.stdout)

    def test_injected_path_rejects_symlink_ancestor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real = root / "real"
            real.mkdir(mode=0o700)
            (real / "private").mkdir(mode=0o700)
            (root / "link").symlink_to(real, target_is_directory=True)
            store = ProfileStore(root / "link" / "private" / "profiles.json")
            with self.assertRaises(ProfileError):
                store.list_names()
            self.assertFalse((real / "private" / "profiles.json").exists())

    @unittest.skipUnless(os.geteuid() == 0, "root-only importer rejection")
    def test_migration_runner_does_not_fall_back_to_checkout_source(self):
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            (checkout / "scripts").mkdir()
            shutil.copy2(repo / "scripts/export-profiles.py", checkout / "scripts")
            shutil.copytree(repo / "src", checkout / "src")
            env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
            result = subprocess.run(
                [sys.executable, "-I", str(checkout / "scripts/export-profiles.py"), "--import"],
                input=b'{"schema":true,"profiles":{}}', capture_output=True,
                env=env, timeout=5, check=False,
            )
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"import requires an unprivileged account", result.stderr)
        self.assertNotIn(b"Traceback", result.stderr)

    def test_migration_runner_loads_installed_zipapp_without_pythonpath(self):
        if os.geteuid() != 0:
            self.skipTest("isolated fixture requires root to run importer as unprivileged user")
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir="/home") as directory:
            location = Path(directory)
            location.chmod(0o755)
            script = location / "okeylitctl-export-profiles"
            archive = location / "okeylitctl.pyz"
            source = (repo / "scripts/export-profiles.py").read_text()
            script.write_text(source.replace("/usr/local/libexec/okeylitctl-export-profiles", str(script))
                                   .replace("/usr/local/bin/okeylitctl", str(archive)))
            script.chmod(0o755)
            zipapp.create_archive(repo / "src", target=archive,
                                  main="okeylitctl.cli:entrypoint")
            archive.chmod(0o755)
            env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
            result = subprocess.run(
                [sys.executable, "-I", str(script), "--import"],
                input=b'{"schema":true,"profiles":{}}', capture_output=True,
                env=env, timeout=5, check=False,
                preexec_fn=lambda: os.setuid(65534),
            )
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"unsupported schema", result.stderr)
        self.assertNotIn(b"Traceback", result.stderr)

    def test_migration_runner_requires_isolated_python_before_imports(self):
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            location = Path(directory)
            (location / "scripts").mkdir()
            script = location / "scripts/export-profiles.py"
            shutil.copy2(repo / "scripts/export-profiles.py", script)
            shutil.copytree(repo / "src", location / "src")
            result = subprocess.run(
                [sys.executable, str(script), "--import"],
                input=b'{"schema":true,"profiles":{}}',
                capture_output=True, timeout=5, check=False,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"-I", result.stderr)
        self.assertNotIn(b"unsupported schema", result.stderr)

    @unittest.skipUnless(os.geteuid() == 0, "root-only importer rejection")
    def test_migration_cli_rejects_root_import_without_loading_checkout(self):
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            (checkout / "scripts").mkdir()
            shutil.copy2(repo / "scripts/export-profiles.py", checkout / "scripts")
            shutil.copytree(repo / "src", checkout / "src")
            result = subprocess.run(
                [sys.executable, "-I", str(checkout / "scripts/export-profiles.py"), "--import"],
                input=b'{"schema":true,"profiles":{}}', capture_output=True,
                env={**os.environ, "PYTHONPATH": str(repo / "src")},
                timeout=5, check=False,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"import requires an unprivileged account", result.stderr)
        self.assertNotIn(b"Traceback", result.stderr)

    @unittest.skipUnless(os.geteuid() == 0, "root-only exporter")
    def test_explicit_export_import_preserves_legacy_and_refuses_existing_destination(self):
        with tempfile.TemporaryDirectory() as old, tempfile.TemporaryDirectory() as home:
            source = ProfileStore(Path(old) / "profiles.json")
            layout = ColorLayout.from_wire("abcdef,123456,abcdef,654321")
            source.save("Legacy", layout)
            old_bytes = source.path.read_bytes()
            with mock.patch("okeylitctl.profiles.pwd.getpwuid", return_value=mock.Mock(pw_dir=home)), mock.patch.dict(os.environ, {"XDG_DATA_HOME": ""}):
                payload = export_legacy_profiles(source.path)
                destination = ProfileStore()
                import_profiles(payload, destination)
                self.assertEqual(destination.load("Legacy"), layout)
                with self.assertRaises(ProfileExists):
                    import_profiles(payload, destination)
                self.assertEqual(source.path.read_bytes(), old_bytes)

    @unittest.skipUnless(os.geteuid() == 0, "root-only exporter")
    def test_export_refuses_symlink_and_unsafe_source(self):
        with tempfile.TemporaryDirectory() as directory:
            source = ProfileStore(Path(directory) / "profiles.json")
            source.save("Safe", ColorLayout.from_wire("111111,222222,333333,444444"))
            os.chmod(source.path, 0o644)
            with self.assertRaises(ProfileError):
                export_legacy_profiles(source.path)
            os.chmod(source.path, 0o600)
            target = source.path.with_name("target.json")
            source.path.rename(target)
            source.path.symlink_to(target)
            with self.assertRaises(ProfileError):
                export_legacy_profiles(source.path)

    def test_import_refuses_existing_symlink_without_modifying_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.json"
            target.write_bytes(b"unchanged")
            link = root / "profiles.json"
            link.symlink_to(target)
            with self.assertRaises(ProfileExists):
                import_profiles(b'{"schema":1,"profiles":{}}', ProfileStore(link))
            self.assertEqual(target.read_bytes(), b"unchanged")

    def test_import_rejects_invalid_schema_and_leaves_destination_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ProfileStore(Path(directory) / "profiles.json")
            for payload in (b'{"schema":true,"profiles":{}}', b'{"schema":1,"profiles":{},"profiles":{}}', b"X" * (64 * 1024 + 1)):
                with self.subTest(payload=payload[:30]), self.assertRaises(ProfileError):
                    import_profiles(payload, store)
            self.assertFalse(store.path.exists())

    def test_default_honors_home_relative_xdg_override_but_not_traversal(self):
        with tempfile.TemporaryDirectory() as home:
            with mock.patch("okeylitctl.profiles.pwd.getpwuid", return_value=mock.Mock(pw_dir=home)):
                inside = Path(home) / "private-data"
                with mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(inside)}):
                    store = ProfileStore()
                    self.assertEqual(store.list_names(), ())
                    self.assertEqual(store.path.parent, inside / "okeylitctl")
                for hostile in ("relative/data", str(Path(home) / ".." / "escape")):
                    with self.subTest(hostile=hostile), mock.patch.dict(os.environ, {"XDG_DATA_HOME": hostile}):
                        self.assertEqual(ProfileStore().path, Path(home) / ".local/share/okeylitctl/profiles.json")

    def test_default_rejects_symlink_ancestor_without_following_it(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as outside:
            (Path(home) / ".local").symlink_to(outside, target_is_directory=True)
            with mock.patch("okeylitctl.profiles.pwd.getpwuid", return_value=mock.Mock(pw_dir=home)), mock.patch.dict(os.environ, {"XDG_DATA_HOME": ""}):
                with self.assertRaises(ProfileError):
                    ProfileStore().list_names()
            self.assertFalse((Path(outside) / "share").exists())

    def test_default_rejects_wrong_owner_of_home(self):
        with tempfile.TemporaryDirectory() as home:
            with mock.patch("okeylitctl.profiles.pwd.getpwuid", return_value=mock.Mock(pw_dir=home)), mock.patch("okeylitctl.profiles.os.geteuid", return_value=os.geteuid() + 1), mock.patch.dict(os.environ, {"XDG_DATA_HOME": ""}):
                with self.assertRaises(ProfileError):
                    ProfileStore().list_names()
            self.assertFalse((Path(home) / ".local").exists())

    def test_default_uses_passwd_home_not_home_environment_and_creates_private_directory(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as hostile:
            with mock.patch("okeylitctl.profiles.pwd.getpwuid", return_value=mock.Mock(pw_dir=home)), mock.patch.dict(os.environ, {"HOME": hostile, "XDG_DATA_HOME": hostile}):
                store = ProfileStore()
                layout = ColorLayout.from_wire("111111,222222,333333,444444")
                store.save("Private", layout)
                self.assertEqual(store.path, Path(home) / ".local/share/okeylitctl/profiles.json")
                self.assertEqual(store.load("Private"), layout)
                self.assertEqual(store.path.parent.stat().st_mode & 0o777, 0o700)
                self.assertFalse((Path(hostile) / "okeylitctl").exists())

    def test_save_list_load_and_delete_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            store = ProfileStore(path)
            layout = ColorLayout.from_wire("abcdef,00ff00,0000ff,123456")

            store.save("Gaming_1", layout)

            self.assertEqual(store.list_names(), ("Gaming_1",))
            self.assertEqual(store.load("Gaming_1"), layout)
            store.rename("Gaming_1", "Work")
            self.assertEqual(store.list_names(), ("Work",))
            self.assertEqual(store.load("Work"), layout)
            with self.assertRaises(ProfileNotFound):
                store.load("Gaming_1")
            store.delete("Work")
            self.assertEqual(store.list_names(), ())

    def test_rejects_invalid_names_and_implicit_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ProfileStore(Path(directory) / "profiles.json")
            layout = ColorLayout.from_wire("111111,222222,333333,444444")

            for name in ("../escape", "with space", "", "a" * 33):
                with self.subTest(name=name), self.assertRaises(ProfileError):
                    store.save(name, layout)

            store.save("Safe-name", layout)
            with self.assertRaises(ProfileExists):
                store.save("Safe-name", layout)

    def test_refuses_symlink_or_malformed_profile_store(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.json"
            target.write_text('{"schema":1,"profiles":{}}\n', encoding="utf-8")
            os.chmod(target, 0o600)
            link = root / "profiles.json"
            link.symlink_to(target)
            with self.assertRaises(ProfileError):
                ProfileStore(link).list_names()

            link.unlink()
            link.write_text("not-json\n", encoding="utf-8")
            os.chmod(link, 0o600)
            with self.assertRaises(ProfileError):
                ProfileStore(link).list_names()

    def test_atomic_store_is_owner_only_and_canonical(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            store = ProfileStore(path)
            store.save(
                "Night",
                ColorLayout.from_wire("abcdef,00ff00,0000ff,123456"),
            )

            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                path.read_text(encoding="utf-8"),
                '{"profiles":{"Night":"ABCDEF,00FF00,0000FF,123456"},"schema":1}\n',
            )

    def test_refuses_group_or_world_access_on_store_and_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "profiles.json"
            store = ProfileStore(path)
            layout = ColorLayout.from_wire("111111,222222,333333,444444")
            store.save("Safe", layout)

            os.chmod(path, 0o644)
            with self.assertRaises(ProfileError):
                store.list_names()

            os.chmod(path, 0o600)
            os.chmod(root, 0o755)
            with self.assertRaises(ProfileError):
                store.save("Other", layout)

    def test_concurrent_saves_cannot_lose_a_successful_update(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ProfileStore(Path(directory) / "profiles.json")
            first_layout = ColorLayout.from_wire("111111,222222,333333,444444")
            second_layout = ColorLayout.from_wire("AAAAAA,BBBBBB,CCCCCC,DDDDDD")
            original_write = store._write
            first_waiting = threading.Event()
            release_first = threading.Event()
            call_count = 0
            call_lock = threading.Lock()
            failures = []

            def delayed_write(profiles):
                nonlocal call_count
                with call_lock:
                    index = call_count
                    call_count += 1
                if index == 0:
                    first_waiting.set()
                    release_first.wait(timeout=2)
                original_write(profiles)

            store._write = delayed_write

            def save(name, layout):
                try:
                    store.save(name, layout)
                except Exception as exc:  # Captured for assertion in the main thread.
                    failures.append(exc)

            first = threading.Thread(target=save, args=("First", first_layout))
            second = threading.Thread(target=save, args=("Second", second_layout))
            first.start()
            self.assertTrue(first_waiting.wait(timeout=2))
            second.start()
            second.join(timeout=0.05)
            release_first.set()
            first.join(timeout=2)
            second.join(timeout=2)

            self.assertEqual(failures, [])
            self.assertEqual(store.list_names(), ("First", "Second"))

    def test_read_is_bounded_even_if_file_grows_after_metadata_check(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            path.write_bytes(b"{" + b" " * (64 * 1024) + b"}")
            os.chmod(path, 0o600)
            metadata = path.stat()
            fake_metadata = mock.Mock(
                st_mode=metadata.st_mode,
                st_uid=metadata.st_uid,
                st_size=1,
            )

            with mock.patch(
                "okeylitctl.profiles.os.fstat", return_value=fake_metadata
            ), mock.patch(
                "okeylitctl.profiles.json.load",
                side_effect=AssertionError("unbounded parser was reached"),
            ):
                with self.assertRaises(ProfileError):
                    ProfileStore(path)._read()

    def test_strict_schema_rejects_duplicate_keys_and_non_integer_version(self):
        payloads = (
            '{"schema":true,"profiles":{}}\n',
            '{"schema":1.0,"profiles":{}}\n',
            '{"schema":1,"profiles":{},"profiles":{}}\n',
        )
        for payload in payloads:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "profiles.json"
                path.write_text(payload, encoding="utf-8")
                os.chmod(path, 0o600)
                with self.assertRaises(ProfileError):
                    ProfileStore(path).list_names()

    def test_recursion_and_read_errors_are_normalized_to_profile_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            path.write_text("[" * 2000 + "0" + "]" * 2000, encoding="utf-8")
            os.chmod(path, 0o600)
            with self.assertRaises(ProfileError):
                ProfileStore(path).list_names()

            path.write_text(
                '{"schema":' + "9" * 5000 + ',"profiles":{}}\n',
                encoding="utf-8",
            )
            os.chmod(path, 0o600)
            with self.assertRaises(ProfileError):
                ProfileStore(path).list_names()

            path.write_text('{"schema":1,"profiles":{}}\n', encoding="utf-8")
            os.chmod(path, 0o600)
            with mock.patch(
                "okeylitctl.profiles.os.read", side_effect=OSError("read failed")
            ):
                with self.assertRaises(ProfileError):
                    ProfileStore(path)._read()


if __name__ == "__main__":
    unittest.main()
