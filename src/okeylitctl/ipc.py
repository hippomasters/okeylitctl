"""Unprivileged, bounded AF_UNIX client for the okeylitctl broker.

One connection carries exactly one SOCK_SEQPACKET request and one response. A
lost acknowledgement after a mutation is *not* retried: its outcome is unknown.
"""
from __future__ import annotations

import errno
import math
import os
import re
import socket
import stat
import struct
from pathlib import Path

from .sysfs import BackendIOError, ModuleUnavailable, PermissionDenied, UnsupportedABI
from .validation import ValidationError, normalize_colors

DEFAULT_SOCKET_PATH = Path("/run/okeylitctl.sock")
_MAX_PACKET = 128
_UNCERTAIN = "mutation outcome uncertain; do not retry automatically"
_WIRE = rb"[0-9A-F]{6}(?:,[0-9A-F]{6}){3}"
_STATUS = re.compile(rb"OK (on|off) (" + _WIRE + rb") (" + _WIRE + rb")\n")
_TOKEN = rb"[0-9A-F]{16}:[0-9A-F]{16}"
_SNAPSHOT = re.compile(rb"OK (" + _TOKEN + rb") (on|off) (" + _WIRE + rb") (" + _WIRE + rb")\n")
_ERRORS = {
    b"INVALID": (ValidationError, "broker rejected invalid request"),
    b"BUSY": (BackendIOError, "broker is busy"),
    b"MODULE": (ModuleUnavailable, "omen_rgb kernel module is unavailable"),
    b"DENIED": (PermissionDenied, "broker denied access"),
    b"IO": (BackendIOError, "broker I/O failure"),
    b"ABI": (UnsupportedABI, "unsupported kernel ABI"),
}


class ConflictError(BackendIOError):
    """The broker definitively rejected CAS without applying the mutation."""


class IPCBackend:
    """Backend-compatible client; the privileged broker alone touches sysfs."""

    def __init__(self, *, socket_path: Path = DEFAULT_SOCKET_PATH, timeout: float = 1.0):
        self.socket_path = Path(socket_path)
        if not self.socket_path.is_absolute():
            raise ValueError("broker socket path must be absolute")
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError("timeout must be positive and finite")
        self.timeout = timeout

    def _socket_identity(self) -> os.stat_result:
        # A symlink at any component defeats a final-component lstat check.
        parent = self.socket_path.parent
        for directory in (parent, *parent.parents):
            try:
                info = directory.lstat()
            except FileNotFoundError:
                raise BackendIOError("broker socket is unavailable") from None
            if (not stat.S_ISDIR(info.st_mode)
                    or info.st_uid not in (0, os.geteuid())
                    or (info.st_mode & 0o022
                        and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX))):
                raise BackendIOError("refusing untrusted broker socket directory")
        try:
            info = self.socket_path.lstat()
        except FileNotFoundError:
            raise BackendIOError("broker socket is unavailable") from None
        if (not stat.S_ISSOCK(info.st_mode)
                or info.st_uid not in ((0,) if self.socket_path == DEFAULT_SOCKET_PATH
                                       else (0, os.geteuid()))
                or info.st_mode & 0o002):
            raise BackendIOError("refusing untrusted broker socket path")
        return info

    def _exchange(self, request: bytes, *, mutation: bool) -> bytes:
        if len(request) > _MAX_PACKET or not request.endswith(b"\n"):
            raise BackendIOError("invalid broker request")
        sent = False
        try:
            identity = self._socket_identity()
            with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as conn:
                conn.settimeout(self.timeout)
                conn.connect(str(self.socket_path))
                current = self._socket_identity()
                if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
                    raise BackendIOError("broker socket changed during connect")
                creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
                if len(creds) != struct.calcsize("3i") or struct.unpack("3i", creds)[1] != identity.st_uid:
                    raise BackendIOError("broker socket peer identity mismatch")
                sent = True  # A failed send may still have delivered the packet.
                written = conn.send(request)
                if written != len(request):
                    raise BackendIOError(f"broker request was not sent completely; {_UNCERTAIN}")
                raw, _ancillary, flags, _address = conn.recvmsg(_MAX_PACKET + 1)
                if len(raw) > _MAX_PACKET or flags & socket.MSG_TRUNC:
                    detail = "broker response exceeds packet limit"
                    raise UnsupportedABI(f"{detail}; {_UNCERTAIN}" if mutation else detail)
                return raw
        except (socket.timeout, TimeoutError) as exc:
            detail = f"broker timed out; {_UNCERTAIN}" if mutation and sent else "broker timed out"
            raise BackendIOError(detail) from None
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EPERM) and not (mutation and sent):
                raise PermissionDenied("broker socket permission denied") from None
            detail = "broker connection failed"
            if mutation and sent:
                detail += f"; {_UNCERTAIN}"
            raise BackendIOError(detail) from None

    @staticmethod
    def _check_error(raw: bytes, *, mutation: bool = False) -> None:
        if raw.startswith(b"ERR ") and raw.endswith(b"\n"):
            code = raw[4:-1]
            if code in _ERRORS:
                error_type, message = _ERRORS[code]
                if mutation and code in (b"IO", b"ABI"):
                    message += f"; {_UNCERTAIN}"
                raise error_type(message)

    def status(self) -> dict[str, object]:
        raw = self._exchange(b"1 STATUS\n", mutation=False)
        self._check_error(raw)
        match = _STATUS.fullmatch(raw)
        if match is None:
            raise UnsupportedABI("malformed broker status response")
        state, colors, original = match.groups()
        return {
            "state": state.decode("ascii"),
            "colors": colors.decode("ascii").split(","),
            "original": original.decode("ascii").split(","),
        }

    def snapshot(self) -> dict[str, object]:
        raw = self._exchange(b"2 SNAPSHOT\n", mutation=False)
        self._check_error(raw)
        match = _SNAPSHOT.fullmatch(raw)
        if match is None:
            raise UnsupportedABI("malformed broker snapshot response")
        token, state, colors, original = match.groups()
        return {
            "token": token.decode("ascii"),
            "state": state.decode("ascii"),
            "colors": colors.decode("ascii").split(","),
            "original": original.decode("ascii").split(","),
        }

    def compare_and_write(
        self, token: str, state: str, expected_wire: str, desired_wire: str
    ) -> str:
        if not isinstance(token, str) or re.fullmatch(r"[0-9A-F]{16}:[0-9A-F]{16}", token) is None:
            raise ValidationError("invalid broker snapshot token")
        if state not in ("on", "off"):
            raise ValidationError("state must be on or off")
        expected = normalize_colors(expected_wire)
        desired = normalize_colors(desired_wire)
        request = f"2 CAS {token} {state} {expected} {desired}\n".encode("ascii")
        raw = self._exchange(request, mutation=True)
        if raw == b"ERR CONFLICT\n":
            raise ConflictError("broker snapshot conflict")
        self._check_error(raw, mutation=True)
        match = re.fullmatch(rb"OK (" + _TOKEN + rb")\n", raw)
        if match is None:
            raise UnsupportedABI(f"malformed broker CAS acknowledgement; {_UNCERTAIN}")
        return match.group(1).decode("ascii")

    def write_colors(self, canonical_colors: str) -> None:
        value = normalize_colors(canonical_colors)
        raw = self._exchange(b"1 SET " + value.encode("ascii") + b"\n", mutation=True)
        self._check_error(raw, mutation=True)
        if raw != b"OK\n":
            raise UnsupportedABI(f"malformed broker write acknowledgement; {_UNCERTAIN}")

    def restore(self) -> None:
        raw = self._exchange(b"1 RESTORE\n", mutation=True)
        self._check_error(raw, mutation=True)
        if raw != b"OK\n":
            raise UnsupportedABI(f"malformed broker restore acknowledgement; {_UNCERTAIN}")
