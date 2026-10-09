"""OKeyLitCtl command-line interface for the omen_rgb kernel module."""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from typing import Callable, Protocol, Sequence, TextIO

from . import __version__
from .ipc import ConflictError, IPCBackend
from .models import ColorLayout, Zone
from .profiles import ProfileError, ProfileStore, normalize_profile_name
from .sysfs import (
    BackendIOError,
    ModuleUnavailable,
    PermissionDenied,
    UnsupportedABI,
)
from .validation import ValidationError, normalize_color, normalize_colors

APP_NAME = "okeylitctl"


class LightingBackend(Protocol):
    """The narrow API shared by the broker and injected test backends."""

    def status(self) -> dict[str, object]: ...
    def write_colors(self, canonical_colors: str) -> None: ...
    def restore(self) -> None: ...


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

    named = commands.add_parser("set", help="update zones by physical name")
    named.add_argument("--all", dest="all_color", action="append", metavar="RRGGBB")
    named.add_argument("--right", action="append", metavar="RRGGBB")
    named.add_argument("--center", action="append", metavar="RRGGBB")
    named.add_argument("--left", action="append", metavar="RRGGBB")
    named.add_argument("--wasd", action="append", metavar="RRGGBB")

    profile = commands.add_parser("profile", help="manage saved local layouts")
    profile_commands = profile.add_subparsers(dest="profile_command", required=True)
    profile_save = profile_commands.add_parser("save", help="save the active layout")
    profile_save.add_argument("name", metavar="NAME")
    profile_load = profile_commands.add_parser("load", help="apply a saved layout")
    profile_load.add_argument("name", metavar="NAME")
    profile_commands.add_parser("list", help="list saved layouts")
    profile_delete = profile_commands.add_parser("delete", help="delete a saved layout")
    profile_delete.add_argument("name", metavar="NAME")

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
    backend: LightingBackend | None = None,
    profile_store: ProfileStore | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    tui_runner: Callable[[LightingBackend], None] | None = None,
) -> int:
    """Run the CLI and return a stable process exit code."""
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    backend = backend if backend is not None else IPCBackend()
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
        elif args.command == "set":
            raw_named_values = {
                Zone.RIGHT: args.right or [],
                Zone.CENTER: args.center or [],
                Zone.LEFT: args.left or [],
                Zone.WASD: args.wasd or [],
            }
            raw_all_values = args.all_color or []

            normalized_named_values = {
                zone: [normalize_color(value) for value in values]
                for zone, values in raw_named_values.items()
            }
            normalized_all_values = [normalize_color(value) for value in raw_all_values]

            repeated = [
                f"--{zone.value.lower()}"
                for zone, values in normalized_named_values.items()
                if len(values) > 1
            ]
            if len(normalized_all_values) > 1:
                repeated.append("--all")
            if repeated:
                raise ValidationError(f"set option cannot be repeated: {', '.join(repeated)}")

            provided = {
                zone: values[0]
                for zone, values in normalized_named_values.items()
                if values
            }
            if normalized_all_values and provided:
                raise ValidationError("set --all cannot be combined with named zones")
            if not normalized_all_values and not provided:
                raise ValidationError("set requires --all or at least one named zone")

            if normalized_all_values:
                color = normalized_all_values[0]
                layout = ColorLayout(color, color, color, color)
                value = layout.to_wire()
                backend.write_colors(value)
            else:
                snapshot = getattr(backend, "snapshot", None)
                compare_and_write = getattr(backend, "compare_and_write", None)
                if callable(snapshot) and callable(compare_and_write):
                    for attempt in range(3):
                        current = snapshot()
                        expected = ColorLayout.from_wire(",".join(current["colors"]))
                        layout = expected
                        for zone in (Zone.RIGHT, Zone.CENTER, Zone.LEFT, Zone.WASD):
                            if zone in provided:
                                layout = layout.with_zone(zone, provided[zone])
                        value = layout.to_wire()
                        try:
                            compare_and_write(current["token"], current["state"],
                                              expected.to_wire(), value)
                            break
                        except ConflictError:
                            if attempt == 2:
                                raise
                else:
                    status = backend.status()
                    layout = ColorLayout.from_wire(",".join(status["colors"]))
                    for zone in (Zone.RIGHT, Zone.CENTER, Zone.LEFT, Zone.WASD):
                        if zone in provided:
                            layout = layout.with_zone(zone, provided[zone])
                    value = layout.to_wire()
                    backend.write_colors(value)
            stdout.write(f"Keyboard colors updated: {value}\n")
        elif args.command == "profile" and args.profile_command == "save":
            store = profile_store or ProfileStore()
            name = normalize_profile_name(args.name)
            status = backend.status()
            layout = ColorLayout.from_wire(",".join(status["colors"]))
            store.save(name, layout)
            stdout.write(f"Saved profile: {name}\n")
        elif args.command == "profile" and args.profile_command == "load":
            store = profile_store or ProfileStore()
            layout = store.load(args.name)
            backend.write_colors(layout.to_wire())
            stdout.write(f"Loaded and applied profile: {args.name}\n")
        elif args.command == "profile" and args.profile_command == "list":
            store = profile_store or ProfileStore()
            for name in store.list_names():
                stdout.write(f"{name}\n")
        elif args.command == "profile" and args.profile_command == "delete":
            store = profile_store or ProfileStore()
            store.delete(args.name)
            stdout.write(f"Deleted profile: {args.name}\n")
        elif args.command == "restore":
            backend.restore()
            stdout.write("Restored the colors saved when the module loaded.\n")
        elif args.command == "tui":
            if tui_runner is None:
                from .tui import run_tui

                run_tui(backend, profile_store=profile_store)
            else:
                tui_runner(backend)
        else:  # argparse requires a known subcommand; retain fail-closed behavior.
            parser.error("unknown command")
        stdout.flush()
        return 0
    except ValidationError as exc:
        stderr.write(f"{APP_NAME}: {exc}\n")
        return 2
    except ProfileError as exc:
        stderr.write(f"{APP_NAME}: {exc}\n")
        return 7
    except ModuleUnavailable as exc:
        stderr.write(f"{APP_NAME}: {exc}\n")
        stderr.write("Hint: load the omen_rgb kernel module first.\n")
        return 3
    except PermissionDenied as exc:
        stderr.write(f"{APP_NAME}: {exc}\n")
        stderr.write("Hint: check access to the okeylitctl broker service and socket.\n")
        return 4
    except BackendIOError as exc:
        stderr.write(f"{APP_NAME}: {exc}\n")
        return 5
    except UnsupportedABI as exc:
        stderr.write(f"{APP_NAME}: unsupported kernel ABI: {exc}\n")
        return 6
    except BrokenPipeError:
        return 0
    except Exception as exc:  # Avoid exposing tracebacks from an installed CLI.
        stderr.write(f"{APP_NAME}: unexpected internal error: {exc}\n")
        return 70


def entrypoint() -> None:
    raise SystemExit(main())
