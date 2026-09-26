"""Narrow, fixed-path access to the omen_rgb kernel ABI."""

from __future__ import annotations

import errno
import os
from pathlib import Path

from .validation import ValidationError, normalize_colors

DEFAULT_ROOT = Path("/sys/bus/platform/devices/omen-rgb")
_ALLOWED = frozenset({"abi_version", "colors", "state", "original", "restore"})
_WRITABLE = frozenset({"colors", "restore"})
_MAX_READ = 128


class SysfsError(RuntimeError):
    """Base class for expected kernel ABI access failures."""


class ModuleUnavailable(SysfsError):
    """The kernel module or one of its required attributes is absent."""


class PermissionDenied(SysfsError):
    """The caller lacks permission to change a root-only attribute."""


class BackendIOError(SysfsError):
    """The kernel rejected or incompletely processed an operation."""


class UnsupportedABI(SysfsError):
    """The module returned data outside the supported ABI."""


def _flags(mode: int) -> int:
    value = mode | getattr(os, "O_CLOEXEC", 0)
    value |= getattr(os, "O_NOFOLLOW", 0)
    return value


class SysfsBackend:
    """Access only the fixed omen_rgb parameter allowlist."""

    def __init__(self, *, _root: Path = DEFAULT_ROOT):
        self._root = Path(_root)

    def _path(self, name: str) -> Path:
        if name not in _ALLOWED:
            raise BackendIOError("refusing unknown kernel parameter")
        return self._root / name

    @staticmethod
    def _translate_error(exc: OSError, operation: str) -> SysfsError:
        if exc.errno in (errno.ENOENT, errno.ENODEV, errno.ENOTDIR):
            return ModuleUnavailable("omen_rgb kernel module is unavailable")
        if exc.errno in (errno.EACCES, errno.EPERM):
            return PermissionDenied(f"permission denied {operation}")
        return BackendIOError(f"kernel ABI {operation} failed: {exc.strerror or exc}")

    def _read(self, name: str) -> str:
        fd = -1
        try:
            fd = os.open(self._path(name), _flags(os.O_RDONLY))
            raw = os.read(fd, _MAX_READ + 1)
        except OSError as exc:
            raise self._translate_error(exc, f"reading {name}") from None
        finally:
            if fd >= 0:
                os.close(fd)

        if len(raw) > _MAX_READ:
            raise UnsupportedABI(f"{name} response is too long")
        if raw.endswith(b"\n"):
            raw = raw[:-1]
        try:
            value = raw.decode("ascii")
        except UnicodeDecodeError:
            raise UnsupportedABI(f"{name} response is not ASCII") from None
        if not value or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
            raise UnsupportedABI(f"{name} response contains control characters")
        return value

    def _write(self, name: str, value: str) -> None:
        if name not in _WRITABLE:
            raise BackendIOError("refusing non-writable kernel parameter")
        try:
            payload = (value + "\n").encode("ascii", "strict")
        except UnicodeEncodeError:
            raise BackendIOError("refusing non-ASCII kernel value") from None
        if len(payload) > 32:
            raise BackendIOError("refusing oversized kernel value")

        fd = -1
        try:
            fd = os.open(self._path(name), _flags(os.O_WRONLY))
            written = os.write(fd, payload)
            if written != len(payload):
                raise BackendIOError(
                    f"short write to {name}: {written} of {len(payload)} bytes"
                )
        except SysfsError:
            raise
        except OSError as exc:
            raise self._translate_error(exc, f"writing {name}") from None
        finally:
            if fd >= 0:
                os.close(fd)

    def _check_abi(self) -> None:
        if self._read("abi_version") != "1":
            raise UnsupportedABI("expected ABI version 1")

    def status(self) -> dict[str, object]:
        self._check_abi()
        try:
            colors = normalize_colors(self._read("colors")).split(",")
            original = normalize_colors(self._read("original")).split(",")
        except ValidationError as exc:
            raise UnsupportedABI(str(exc)) from None
        state_raw = self._read("state")
        if state_raw not in ("on", "off"):
            raise UnsupportedABI("state response must be on or off")
        return {
            "state": state_raw,
            "colors": colors,
            "original": original,
        }

    def write_colors(self, canonical_colors: str) -> None:
        self._check_abi()
        self._write("colors", canonical_colors)
        actual = normalize_colors(self._read("colors"))
        if actual != canonical_colors:
            raise UnsupportedABI("firmware color readback did not match the request")

    def restore(self) -> None:
        self._check_abi()
        expected = normalize_colors(self._read("original"))
        self._write("restore", "1")
        actual = normalize_colors(self._read("colors"))
        if actual != expected:
            raise UnsupportedABI("restored colors did not match the saved original")
