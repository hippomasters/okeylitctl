#!/usr/bin/env python3
"""Root-only, reversible, explicitly staged broker rate experiment.

Never run this script from a user-writable checkout with sudo. Copy it and the
reviewed artifacts into a root-owned 0700 staging directory first, and compare
all supplied SHA-256 values with the independent review before invoking it.
This only changes the broker binary/unit, not the driver or client installation.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile

BROKER = Path("/usr/local/libexec/okeylitctl-broker")
SERVICE = Path("/etc/systemd/system/okeylitctl-broker.service")
MANIFEST = Path("/usr/src/omen-rgb-0.1.0/.okeylitctl-manifest")
STATE = Path("/var/lib/okeylitctl-rate-experiment-2")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def trusted(path, mode):
    path = Path(path)
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != 0 or \
            stat.S_IMODE(st.st_mode) != mode:
        raise ValueError(f"unsafe file: {path}")
    return path


def state_dir():
    st = STATE.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != 0 or \
            stat.S_IMODE(st.st_mode) != 0o700:
        raise ValueError("unsafe experiment state directory")


def present(path):
    # Path.exists() follows symlinks and misses a dangling hostile entry.
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


def systemctl(*args):
    subprocess.run(("systemctl", *args), check=True)


def checked(path, mode, expected):
    path = trusted(path, mode)
    if len(expected) != 64 or digest(path) != expected:
        raise ValueError(f"unexpected SHA-256: {path}")
    return path.read_bytes()


def replace(path, data, mode):
    # Atomic same-directory replacement; no shell redirection through a symlink.
    fd, name = tempfile.mkstemp(prefix=".okeylitctl-rate-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            os.fchmod(out.fileno(), mode)
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def stop():
    systemctl("stop", "okeylitctl-broker.socket")
    systemctl("stop", "okeylitctl-broker.service")


def start():
    systemctl("daemon-reload")
    systemctl("start", "okeylitctl-broker.socket")


def receipt():
    raw = trusted(STATE / "receipt", 0o600).read_bytes()
    if len(raw) > 1024:
        raise ValueError("oversized experiment receipt")
    data = json.loads(raw)
    if set(data) != {"ordinary_broker", "ordinary_service", "diagnostic_broker",
                     "diagnostic_service"} or any(not isinstance(v, str) or len(v) != 64
                                                   for v in data.values()):
        raise ValueError("invalid experiment receipt")
    return data


def deploy(diagnostic, diagnostic_hash, unit, unit_hash, broker_hash, service_hash):
    state_dir()
    if any(present(STATE / name) for name in
           ("receipt", "used", "rearms", "attempted", "ordinary-broker",
            "ordinary-service")):
        raise ValueError("prior experiment is present; restore/review before staging")
    stage = Path(diagnostic).parent
    st = stage.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != 0 or stat.S_IMODE(st.st_mode) != 0o700 \
            or Path(unit).parent != stage:
        raise ValueError("artifacts must share a root-owned 0700 staging directory")
    diag_data = checked(diagnostic, 0o755, diagnostic_hash)
    unit_data = checked(unit, 0o644, unit_hash)
    ordinary = checked(BROKER, 0o755, broker_hash)
    ordinary_unit = checked(SERVICE, 0o644, service_hash)
    manifest = trusted(MANIFEST, 0o600).read_bytes()
    entries = manifest.decode("ascii").splitlines()
    if len(entries) != 11 or entries[0] != "okeylitctl-manifest-v2" or \
            entries[7:9] != [broker_hash, service_hash]:
        raise ValueError("installed ordinary broker/service differ from installer manifest")
    hashes = dict(ordinary_broker=broker_hash, ordinary_service=service_hash,
                  diagnostic_broker=diagnostic_hash, diagnostic_service=unit_hash)
    # A durable one-shot marker also blocks restaging after interrupted rollback.
    replace(STATE / "attempted", b"1", 0o600)
    try:
        stop()
        replace(STATE / "ordinary-broker", ordinary, 0o600)
        replace(STATE / "ordinary-service", ordinary_unit, 0o600)
        replace(STATE / "receipt", json.dumps(hashes).encode(), 0o600)
        replace(BROKER, diag_data, 0o755)
        replace(SERVICE, unit_data, 0o644)
        start()
    except BaseException:
        # Before a receipt exists no installed file should have changed. Verify
        # that invariant before restarting; retain all partial recovery files.
        if present(STATE / "receipt"):
            restore()
        else:
            checked(BROKER, 0o755, broker_hash)
            checked(SERVICE, 0o644, service_hash)
            start()
        raise


def restore():
    state_dir()
    hashes = receipt()
    ordinary = checked(STATE / "ordinary-broker", 0o600, hashes["ordinary_broker"])
    ordinary_unit = checked(STATE / "ordinary-service", 0o600, hashes["ordinary_service"])
    # Reject unrelated modifications rather than overwriting another installation.
    if digest(trusted(BROKER, 0o755)) not in (hashes["ordinary_broker"], hashes["diagnostic_broker"]) or \
            digest(trusted(SERVICE, 0o644)) not in (hashes["ordinary_service"], hashes["diagnostic_service"]):
        raise ValueError("installed broker or service changed: manual recovery required")
    stop()
    replace(BROKER, ordinary, 0o755)
    replace(SERVICE, ordinary_unit, 0o644)
    start()
    # Do not remove the consumed stamp: a stale diagnostic binary cannot be rearmed.
    (STATE / "receipt").unlink()
    (STATE / "ordinary-broker").unlink()
    (STATE / "ordinary-service").unlink()
    directory = os.open(STATE, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def rearm():
    state_dir()
    directory = os.open(STATE, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        fcntl.flock(directory, fcntl.LOCK_EX)
        hashes = receipt()
        checked(BROKER, 0o755, hashes["diagnostic_broker"])
        checked(SERVICE, 0o644, hashes["diagnostic_service"])
        if present(STATE / "rearms"):
            raise ValueError("additional diagnostic lease already used")
        stamp = trusted(STATE / "used", 0o600)
        # Charge the only rearm before changing service state. A crash or failed
        # restart cannot refund the lease; the consumed stamp is unlinked only
        # with the service stopped.
        replace(STATE / "rearms", b"1", 0o600)
        try:
            stop()
            stamp.unlink()
            start()
        except BaseException:
            # Retain the charged marker even if startup fails.
            start()
            raise
    finally:
        os.close(directory)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    install = sub.add_parser("deploy")
    for name in ("diagnostic", "diagnostic_hash", "unit", "unit_hash",
                 "broker_hash", "service_hash"):
        install.add_argument(name)
    sub.add_parser("restore")
    sub.add_parser("rearm")
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        parser.error("root required; do not execute from a user-writable checkout")
    script = Path(__file__)
    if script != STATE / "stage/deploy.py":
        raise ValueError("run only the reviewed root-staged deployment tool")
    stage = script.parent
    st = stage.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != 0 or \
            stat.S_IMODE(st.st_mode) != 0o700:
        raise ValueError("unsafe deployment tool directory")
    trusted(script, 0o644)
    if args.action == "deploy":
        deploy(args.diagnostic, args.diagnostic_hash, args.unit, args.unit_hash,
               args.broker_hash, args.service_hash)
    elif args.action == "restore":
        restore()
    else:
        rearm()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        sys.exit(f"rate experiment: {exc}")
