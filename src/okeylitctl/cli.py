"""OKeyLitCtl command-line interface for the omen_rgb kernel module."""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from typing import Callable, Sequence, TextIO

from . import __version__
from .sysfs import (
    BackendIOError,
    ModuleUnavailable,
    PermissionDenied,
    SysfsBackend,
    UnsupportedABI,
)
from .validation import ValidationError, normalize_colors

APP_NAME = "okeylitctl"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description="Safely control supported HP OMEN four-zone keyboard lighting.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    status = commands.add_parser("status", help="show the current keyboard state")
    status.add_argument("--json", action="store_true", dest="as_json")

    colors = commands.add_parser("colors", help="set all four RGB zones atomically")
    colors.add_argument("value", metavar="RRGGBB,RRGGBB,RRGGBB,RRGGBB")

    commands.add_parser("restore", help="restore colors saved when the module loaded")
    commands.add_parser("tui", help="open the interactive four-zone workspace")
    return parser


def _print_status(status: dict[str, object], stream: TextIO) -> None:
    stream.write(f"State:    {status['state']}\n")
    stream.write(f"Colors:   {','.join(status['colors'])}\n")
    stream.write(f"Original: {','.join(status['original'])}\n")


def main(
    argv: Sequence[str] | None = None,
    *,
    backend: SysfsBackend | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    tui_runner: Callable[[SysfsBackend], None] | None = None,
) -> int:
    """Run the CLI and return a stable process exit code."""
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    backend = backend or SysfsBackend()
    parser = _parser()

    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)

    try:
        if args.command == "status":
            status = backend.status()
            if args.as_json:
                json.dump(status, stdout, sort_keys=True)
                stdout.write("\n")
            else:
                _print_status(status, stdout)
        elif args.command == "colors":
            value = normalize_colors(args.value)
            backend.write_colors(value)
            stdout.write(f"Keyboard colors updated: {value}\n")
        elif args.command == "restore":
            backend.restore()
            stdout.write("Restored the colors saved when the module loaded.\n")
        elif args.command == "tui":
            if tui_runner is None:
                from .tui import run_tui

                tui_runner = run_tui
            tui_runner(backend)
        else:  # argparse requires a known subcommand; retain fail-closed behavior.
            parser.error("unknown command")
        stdout.flush()
        return 0
    except ValidationError as exc:
        stderr.write(f"{APP_NAME}: {exc}\n")
        return 2
    except ModuleUnavailable as exc:
        stderr.write(f"{APP_NAME}: {exc}\n")
        stderr.write("Hint: load the omen_rgb kernel module first.\n")
        return 3
    except PermissionDenied as exc:
        stderr.write(f"{APP_NAME}: {exc}\n")
        stderr.write("Hint: mutation commands must be run as root (for example, with sudo).\n")
        return 4
    except BackendIOError as exc:
        stderr.write(f"{APP_NAME}: {exc}\n")
        return 5
    except UnsupportedABI as exc:
        stderr.write(f"{APP_NAME}: unsupported kernel ABI: {exc}\n")
        return 6
    except BrokenPipeError:
        return 0
    except Exception as exc:  # Avoid exposing tracebacks from an installed root CLI.
        stderr.write(f"{APP_NAME}: unexpected internal error: {exc}\n")
        return 70


def entrypoint() -> None:
    raise SystemExit(main())
