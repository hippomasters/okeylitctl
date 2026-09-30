import tempfile
import unittest
import os
import threading
from pathlib import Path
from unittest import mock

from okeylitctl.models import ColorLayout
from okeylitctl.profiles import ProfileError, ProfileExists, ProfileNotFound, ProfileStore


class ProfileStoreTests(unittest.TestCase):
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
