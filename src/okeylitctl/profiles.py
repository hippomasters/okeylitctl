"""Root-owned local color profiles with fixed-schema atomic storage."""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import fcntl
from contextlib import contextmanager
from pathlib import Path

from .models import ColorLayout

PROFILE_PATH = Path("/var/lib/okeylitctl/profiles.json")
_PROFILE_SCHEMA = 1
_PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}\Z")
_MAX_PROFILES = 64
_MAX_FILE_SIZE = 64 * 1024


class ProfileError(Exception):
    """Base error for safe profile operations."""


class ProfileNotFound(ProfileError):
    """Requested profile does not exist."""


class ProfileExists(ProfileError):
    """Requested profile already exists."""


def normalize_profile_name(name: str) -> str:
    """Validate an exact, display-safe profile identifier."""
    if not _PROFILE_NAME.fullmatch(name):
        raise ProfileError(
            "profile name must be 1-32 letters, digits, underscores, or hyphens"
        )
    return name


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ProfileError(f"profile store contains duplicate key: {key}")
        result[key] = value
    return result


class ProfileStore:
    """Read and atomically update a bounded profile document."""

    def __init__(self, path: Path = PROFILE_PATH):
        self.path = Path(path)

    def list_names(self) -> tuple[str, ...]:
        with self._lock(exclusive=False):
            return tuple(sorted(self._read()))

    def load(self, name: str) -> ColorLayout:
        name = normalize_profile_name(name)
        with self._lock(exclusive=False):
            profiles = self._read()
            try:
                return profiles[name]
            except KeyError as exc:
                raise ProfileNotFound(f"profile not found: {name}") from exc

    def save(self, name: str, layout: ColorLayout, *, overwrite: bool = False) -> None:
        name = normalize_profile_name(name)
        with self._lock(exclusive=True):
            profiles = self._read()
            if name in profiles and not overwrite:
                raise ProfileExists(f"profile already exists: {name}")
            if name not in profiles and len(profiles) >= _MAX_PROFILES:
                raise ProfileError(f"profile limit reached ({_MAX_PROFILES})")
            profiles[name] = layout
            self._write(profiles)

    def delete(self, name: str) -> None:
        name = normalize_profile_name(name)
        with self._lock(exclusive=True):
            profiles = self._read()
            if name not in profiles:
                raise ProfileNotFound(f"profile not found: {name}")
            del profiles[name]
            self._write(profiles)

    @contextmanager
    def _lock(self, *, exclusive: bool):
        parent = self._safe_parent()
        lock_path = parent / f".{self.path.name}.lock"
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise ProfileError(f"cannot safely open profile lock: {exc.strerror}") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ProfileError("profile lock is not a regular file")
            if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
                raise ProfileError("profile lock ownership or permissions are unsafe")
            try:
                fcntl.flock(
                    descriptor,
                    fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH,
                )
            except OSError as exc:
                raise ProfileError(f"cannot lock profile store: {exc.strerror}") from exc
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(descriptor)

    def _safe_parent(self) -> Path:
        parent = self.path.parent
        try:
            metadata = parent.stat(follow_symlinks=False)
        except OSError as exc:
            raise ProfileError(f"profile directory is unavailable: {exc.strerror}") from exc
        if not stat.S_ISDIR(metadata.st_mode):
            raise ProfileError("profile directory is not a real directory")
        if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
            raise ProfileError("profile directory ownership or permissions are unsafe")
        return parent

    def _read(self) -> dict[str, ColorLayout]:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.path, flags)
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise ProfileError(f"cannot safely open profile store: {exc.strerror}") from exc

        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ProfileError("profile store is not a regular file")
            if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
                raise ProfileError("profile store ownership or permissions are unsafe")
            if metadata.st_size > _MAX_FILE_SIZE:
                raise ProfileError("profile store exceeds the size limit")
            chunks: list[bytes] = []
            remaining = _MAX_FILE_SIZE + 1
            while remaining:
                chunk = os.read(descriptor, min(8192, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
            if len(payload) > _MAX_FILE_SIZE:
                raise ProfileError("profile store exceeds the size limit")
            document = json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
            )
        except ProfileError:
            raise
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            RecursionError,
            ValueError,
        ) as exc:
            raise ProfileError("profile store is malformed") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)

        if not isinstance(document, dict) or set(document) != {"schema", "profiles"}:
            raise ProfileError("profile store has an unsupported structure")
        if (
            type(document["schema"]) is not int
            or document["schema"] != _PROFILE_SCHEMA
            or not isinstance(document["profiles"], dict)
        ):
            raise ProfileError("profile store has an unsupported schema")
        if len(document["profiles"]) > _MAX_PROFILES:
            raise ProfileError("profile store contains too many profiles")

        profiles: dict[str, ColorLayout] = {}
        for name, wire in document["profiles"].items():
            normalize_profile_name(name)
            if not isinstance(wire, str):
                raise ProfileError("profile layout must be a color string")
            try:
                profiles[name] = ColorLayout.from_wire(wire)
            except (TypeError, ValueError) as exc:
                raise ProfileError(f"profile has an invalid layout: {name}") from exc
        return profiles

    def _write(self, profiles: dict[str, ColorLayout]) -> None:
        parent = self._safe_parent()

        document = {
            "schema": _PROFILE_SCHEMA,
            "profiles": {name: profiles[name].to_wire() for name in sorted(profiles)},
        }
        payload = json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
        if len(payload.encode("utf-8")) > _MAX_FILE_SIZE:
            raise ProfileError("profile store exceeds the size limit")

        descriptor = -1
        temporary = ""
        try:
            descriptor, temporary = tempfile.mkstemp(
                dir=parent, prefix=".profiles.", suffix=".tmp", text=True
            )
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                descriptor = -1
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = ""
            directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            raise ProfileError(f"cannot update profile store: {exc.strerror}") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
