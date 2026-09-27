#!/bin/sh
set -eu

PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
unset PYTHONPATH PYTHONHOME PYTHONSTARTUP PYTHONINSPECT
umask 022

APP_VERSION="0.2.0"
DRIVER_VERSION="0.1.0"
MODULE="omen-rgb"
DEST="/usr/src/${MODULE}-${DRIVER_VERSION}"
CLI_DEST="/usr/local/bin/okeylitctl"
LEGACY_CLI_DEST="/usr/local/bin/omen-rgb"
LOAD_DEST="/etc/modules-load.d/omen-rgb.conf"
PROFILE_DIR="/var/lib/okeylitctl"
TMP=""
COMPLETE=0
CREATED_DEST=0
DKMS_ADDED=0
INSTALLED_CLI=0
INSTALLED_LOAD=0
MAY_HAVE_LOADED=0
CREATED_PROFILE_DIR=0

cleanup()
{
    code=$?
    trap - 0 HUP INT TERM
    [ -z "$TMP" ] || rm -f -- "$TMP"
    if [ "$COMPLETE" -ne 1 ]; then
        if [ "$MAY_HAVE_LOADED" -eq 1 ]; then
            modprobe -r omen_rgb >/dev/null 2>&1 || true
        fi
        [ "$INSTALLED_LOAD" -ne 1 ] || rm -f -- "$LOAD_DEST"
        [ "$INSTALLED_CLI" -ne 1 ] || rm -f -- "$CLI_DEST"
        if [ "$CREATED_PROFILE_DIR" -eq 1 ] && [ -d "$PROFILE_DIR" ] && [ ! -L "$PROFILE_DIR" ]; then
            rmdir -- "$PROFILE_DIR" 2>/dev/null || true
        fi
        if [ "$DKMS_ADDED" -eq 1 ]; then
            dkms remove -m "$MODULE" -v "$DRIVER_VERSION" --all >/dev/null 2>&1 || true
        fi
        if [ "$CREATED_DEST" -eq 1 ] && [ -d "$DEST" ] && [ ! -L "$DEST" ]; then
            rm -rf -- "$DEST"
        fi
    fi
    exit "$code"
}
trap cleanup 0
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

if [ "$(id -u)" -ne 0 ]; then
    printf '%s\n' "install-dkms.sh must be run as root" >&2
    exit 1
fi
for command in dkms make python3 install modprobe mktemp stat; do
    command -v "$command" >/dev/null 2>&1 || {
        printf 'missing required command: %s\n' "$command" >&2
        exit 1
    }
done
if [ -d /sys/module/omen_rgb ]; then
    printf '%s\n' "omen_rgb is already loaded; unload it before installing" >&2
    exit 1
fi

if [ -L "$PROFILE_DIR" ] || { [ -e "$PROFILE_DIR" ] && [ ! -d "$PROFILE_DIR" ]; }; then
    printf 'refusing unsafe profile path: %s\n' "$PROFILE_DIR" >&2
    exit 1
fi
if [ -d "$PROFILE_DIR" ]; then
    if [ "$(stat -c %u -- "$PROFILE_DIR")" != 0 ] || \
       [ "$(stat -c %a -- "$PROFILE_DIR")" != 700 ]; then
        printf 'refusing unsafe profile directory ownership or mode: %s\n' "$PROFILE_DIR" >&2
        exit 1
    fi
else
    install -d -o root -g root -m 0700 "$PROFILE_DIR"
    CREATED_PROFILE_DIR=1
fi

SELF_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
SOURCE=$(CDPATH= cd -- "${SELF_DIR}/.." && pwd -P)
for path in "$DEST" "$CLI_DEST" "$LEGACY_CLI_DEST" "$LOAD_DEST"; do
    if [ -e "$path" ] || [ -L "$path" ]; then
        printf 'refusing existing installed path: %s\n' "$path" >&2
        printf '%s\n' "remove the existing version deliberately before reinstalling" >&2
        exit 1
    fi
done

install -d -m 0755 "$DEST/module"
CREATED_DEST=1
install -m 0644 "$SOURCE/Makefile" "$SOURCE/dkms.conf" "$DEST/"
install -m 0644 "$SOURCE/module/omen_rgb.c" \
    "$SOURCE/module/omen_rgb_protocol.h" "$DEST/module/"
chown -R root:root "$DEST"
chmod -R go-w "$DEST"

dkms add -m "$MODULE" -v "$DRIVER_VERSION"
DKMS_ADDED=1
dkms build -m "$MODULE" -v "$DRIVER_VERSION"
dkms install -m "$MODULE" -v "$DRIVER_VERSION"

TMP=$(mktemp /tmp/okeylitctl.XXXXXX)
python3 -I -m zipapp "$SOURCE/src" -m okeylitctl.cli:entrypoint \
    -p /usr/bin/python3 -o "$TMP"
install -m 0755 "$TMP" "$CLI_DEST"
INSTALLED_CLI=1
install -m 0644 "$SOURCE/omen-rgb.modules-load.conf" "$LOAD_DEST"
INSTALLED_LOAD=1

MAY_HAVE_LOADED=1
modprobe omen_rgb
COMPLETE=1
printf '%s\n' "OKeyLitCtl ${APP_VERSION} installed; omen_rgb ${DRIVER_VERSION} loaded"
