#!/usr/bin/env python3
"""Explicit one-time legacy profile migration (no firmware access).

Run the installed, root-owned helper in both interpreters. The administrator
must authorize exposing the old system-wide profiles to the selected account.
Never invoke a script from a user-writable checkout through sudo.

    sudo -v
    set -o pipefail  # Bash
    sudo -n /usr/bin/python3 -I /usr/local/libexec/okeylitctl-export-profiles --export | /usr/bin/python3 -I /usr/local/libexec/okeylitctl-export-profiles --import

The left process reads /var/lib/okeylitctl/profiles.json as root; the right
process runs as the intended unprivileged account. The old store is never
changed. An existing destination is never overwritten; keep the exported data
secure if a retry is needed. Avoid redirecting the JSON into a shared file.
"""

import sys

if not sys.flags.isolated:
    print("profile migration: run with python3 -I (isolated mode)", file=sys.stderr)
    sys.exit(2)

import argparse
import os
import stat
from pathlib import Path

HELPER = Path("/usr/local/libexec/okeylitctl-export-profiles")
APP_ARCHIVE = Path("/usr/local/bin/okeylitctl")


def _trusted_import_path(path: Path, *, archive: bool) -> None:
    """Require root-owned, non-writable components without following links."""
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("package location must be absolute without traversal")
    current = Path("/")
    for component in path.parts[1:]:
        current /= component
        metadata = current.lstat()  # Reject symlinks at every level.
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"symlink in package location: {current}")
        if metadata.st_uid != 0 or metadata.st_mode & 0o022:
            raise ValueError(f"unsafe package ownership or permissions: {current}")
    if archive and not stat.S_ISREG(metadata.st_mode):
        raise ValueError("installed app archive is not a regular file")
    if not archive and not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("expected a directory")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--export", action="store_true", help="root-only legacy export to stdout")
    operation.add_argument("--import", dest="import_", action="store_true", help="user-only import from stdin")
    args = parser.parse_args()
    try:
        if os.geteuid() == 0:
            if not args.export:
                raise ValueError("import requires an unprivileged account")
            # This check is defense in depth: never *invoke* a checkout script
            # under sudo, since Python executes it before this guard can run.
            if Path(__file__).absolute() != HELPER:
                raise ValueError("root export requires the installed helper")
            _trusted_import_path(HELPER, archive=True)
            if HELPER.stat().st_mode & 0o111 != 0o111:
                raise ValueError("installed helper must be executable")
        elif args.export:
            raise ValueError("export requires root")
        _trusted_import_path(APP_ARCHIVE, archive=True)
        sys.path.insert(0, str(APP_ARCHIVE))
        from okeylitctl.profiles import (
            ProfileError, _MAX_FILE_SIZE, _decode_profiles,
            export_legacy_profiles, import_profiles,
        )
    except (OSError, ValueError) as exc:
        print(f"profile migration: cannot load trusted package: {exc}", file=sys.stderr)
        return 1
    try:
        if args.export:
            sys.stdout.buffer.write(export_legacy_profiles())
        else:
            payload = sys.stdin.buffer.read(_MAX_FILE_SIZE + 1)
            _decode_profiles(payload)
            import_profiles(payload)
    except (ProfileError, BrokenPipeError) as exc:
        print(f"profile migration: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
