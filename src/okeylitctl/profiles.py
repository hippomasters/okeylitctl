"""Owner-only local color profiles with fixed-schema atomic storage."""

from __future__ import annotations

import json
import os
import pwd
import re
import stat
import tempfile
import fcntl
from contextlib import contextmanager
from pathlib import Path

from .models import ColorLayout

# Legacy root-owned profiles are deliberately not read by the unprivileged app.
LEGACY_PROFILE_PATH = Path("/var/lib/okeylitctl/profiles.json")
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


def _default_profile_location() -> tuple[Path, Path]:
    """Select a data directory under the account's real home, not $HOME."""
    try:
        home = Path(pwd.getpwuid(os.geteuid()).pw_dir)
    except (KeyError, OSError) as exc:
        raise ProfileError("account home directory is unavailable") from exc
    if not home.is_absolute() or ".." in home.parts or home == Path("/"):
        raise ProfileError("account home directory is unsafe")
    base = home / ".local" / "share"
    override = os.environ.get("XDG_DATA_HOME")
    if override:
        candidate = Path(override)
        # Never let untrusted environment select another account's data or
        # an arbitrary writable tree such as /tmp.
        if candidate.is_absolute() and ".." not in candidate.parts and candidate != home:
            try:
                candidate.relative_to(home)
            except ValueError:
                pass
            else:
                base = candidate
    return home, base / "okeylitctl" / "profiles.json"


def _prepare_private_parent(home: Path, parent: Path) -> None:
    """Create home-relative directories without following symlink components."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open("/", flags)
    try:
        home_parts = home.parts[1:]
        for index, part in enumerate(parent.parts[1:]):
            if part in (".", ".."):
                raise ProfileError("profile directory contains traversal")
            below_home = index >= len(home_parts)
            if below_home:
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            next_descriptor = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
            metadata = os.fstat(descriptor)
            if index == len(home_parts) - 1 or below_home:
                if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022:
                    raise ProfileError("account data directory ownership or permissions are unsafe")
            elif metadata.st_uid not in (0, os.geteuid()) or (
                metadata.st_mode & 0o022 and not
                (metadata.st_uid == 0 and metadata.st_mode & stat.S_ISVTX)
            ):
                raise ProfileError("account home ancestor is unsafe")
    except OSError as exc:
        raise ProfileError(f"profile directory is unavailable: {exc.strerror}") from exc
    finally:
        os.close(descriptor)


def _decode_profiles(payload: bytes) -> dict[str, ColorLayout]:
    if len(payload) > _MAX_FILE_SIZE:
        raise ProfileError("profile store exceeds the size limit")
    try:
        document = json.loads(payload.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except ProfileError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ProfileError("profile store is malformed") from exc
    if not isinstance(document, dict) or set(document) != {"schema", "profiles"}:
        raise ProfileError("profile store has an unsupported structure")
    if (type(document["schema"]) is not int or document["schema"] != _PROFILE_SCHEMA
            or not isinstance(document["profiles"], dict)):
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


def _encode_profiles(profiles: dict[str, ColorLayout]) -> bytes:
    document = {"schema": _PROFILE_SCHEMA,
                "profiles": {name: profiles[name].to_wire() for name in sorted(profiles)}}
    payload = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if len(payload) > _MAX_FILE_SIZE:
        raise ProfileError("profile store exceeds the size limit")
    return payload


class ProfileStore:
    """Read and atomically update a bounded profile document."""

    def __init__(self, path: Path | None = None):
        self._home = None
        if path is None:
            self._home, path = _default_profile_location()
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

    def rename(self, old_name: str, new_name: str) -> None:
        old_name = normalize_profile_name(old_name)
        new_name = normalize_profile_name(new_name)
        with self._lock(exclusive=True):
            profiles = self._read()
            if old_name not in profiles:
                raise ProfileNotFound(f"profile not found: {old_name}")
            if new_name != old_name and new_name in profiles:
                raise ProfileExists(f"profile already exists: {new_name}")
            if new_name == old_name:
                return
            profiles[new_name] = profiles.pop(old_name)
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
            | os.O_NONBLOCK
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
        if self._home is not None:
            _prepare_private_parent(self._home, parent)
        elif not parent.is_absolute() or ".." in self.path.parts:
            raise ProfileError("profile directory must be an absolute path without traversal")
        else:
            # Explicit paths (including the administrator's legacy store) must
            # not enter a symlinked ancestor on their way to the private parent.
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
            descriptor = os.open("/", flags)
            try:
                for part in parent.parts[1:]:
                    next_descriptor = os.open(part, flags, dir_fd=descriptor)
                    os.close(descriptor)
                    descriptor = next_descriptor
                    metadata = os.fstat(descriptor)
                    if metadata.st_uid not in (0, os.geteuid()) or (
                        metadata.st_mode & 0o022 and not
                        (metadata.st_uid == 0 and metadata.st_mode & stat.S_ISVTX)
                    ):
                        raise ProfileError("profile directory ancestor is unsafe")
            except OSError as exc:
                raise ProfileError(f"profile directory is unavailable: {exc.strerror}") from exc
            finally:
                os.close(descriptor)
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
        # Opening a FIFO for reading blocks before fstat can reject it.
        flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
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
        except OSError as exc:
            raise ProfileError("profile store is malformed") from exc
        finally:
            os.close(descriptor)

        return _decode_profiles(payload)

    def _write(self, profiles: dict[str, ColorLayout]) -> None:
        parent = self._safe_parent()

        payload = _encode_profiles(profiles)

        descriptor = -1
        temporary = ""
        try:
            descriptor, temporary = tempfile.mkstemp(
                dir=parent, prefix=".profiles.", suffix=".tmp", text=True
            )
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
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


def export_legacy_profiles(source: Path = LEGACY_PROFILE_PATH) -> bytes:
    """Explicit administrator export; never used by ordinary app operations."""
    if os.geteuid() != 0:
        raise ProfileError("legacy export requires administrator privileges")
    store = ProfileStore(source)
    with store._lock(exclusive=False):
        try:
            os.lstat(store.path)
        except FileNotFoundError as exc:
            raise ProfileError("legacy profile store does not exist") from exc
        except OSError as exc:
            raise ProfileError(f"cannot inspect legacy profile store: {exc.strerror}") from exc
        return _encode_profiles(store._read())


def import_profiles(payload: bytes, destination: ProfileStore | None = None) -> None:
    """Import a strict snapshot only into an absent owner-only store."""
    profiles = _decode_profiles(payload)
    store = destination if destination is not None else ProfileStore()
    with store._lock(exclusive=True):
        try:
            os.lstat(store.path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ProfileError(f"cannot inspect destination: {exc.strerror}") from exc
        else:
            raise ProfileExists("destination profile store already exists; refusing overwrite")
        store._write(profiles)
