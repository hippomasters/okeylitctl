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
BROKER_DEST="/usr/local/libexec/okeylitctl-broker"
MIGRATION_DEST="/usr/local/libexec/okeylitctl-export-profiles"
BROKER_DIR="/usr/local/libexec"
SERVICE_DEST="/etc/systemd/system/okeylitctl-broker.service"
SOCKET_DEST="/etc/systemd/system/okeylitctl-broker.socket"
GROUP=okeylitctl
MANIFEST="$DEST/.okeylitctl-manifest"

if [ "$(id -u)" -ne 0 ]; then
    printf '%s\n' 'install-dkms.sh must be run as root' >&2
    exit 1
fi

# Enrollment is a separate, explicitly requested operation on an existing install.
# Never infer a user from privilege-escalation environment variables.
if [ "$#" -ne 0 ]; then
    case "$1" in
        --accept-existing-group)
            if [ "$#" -eq 1 ]; then
                ACCEPT_EXISTING_GROUP=1
                shift
            fi ;;
        --allow-user) ;;
        *) printf '%s\n' 'usage: install-dkms.sh [--accept-existing-group | --allow-user USER]' >&2; exit 2 ;;
    esac
fi
if [ "$#" -ne 0 ]; then
    if [ "$#" -ne 2 ] || [ "$1" != --allow-user ]; then
        printf '%s\n' 'usage: install-dkms.sh [--accept-existing-group | --allow-user USER]' >&2
        exit 2
    fi
    case "$2" in
        ''|[!a-zA-Z_]*|*[!a-zA-Z0-9_-]*)
            printf '%s\n' 'invalid user name' >&2; exit 2 ;;
    esac
    for command in getent id usermod stat; do
        command -v "$command" >/dev/null 2>&1 || exit 1
    done
    getent passwd "$2" >/dev/null || { printf '%s\n' 'unknown user' >&2; exit 1; }
    getent group "$GROUP" >/dev/null || { printf '%s\n' 'broker group is missing' >&2; exit 1; }
    for path in "$CLI_DEST" "$BROKER_DEST" "$SOCKET_DEST" "$SERVICE_DEST"; do
        if [ -L "$path" ] || [ ! -f "$path" ] ||
           [ "$(stat -c %u -- "$path")" != 0 ]; then
            printf 'broker installation missing or unsafe: %s\n' "$path" >&2
            exit 1
        fi
    done
    if ! id -nG "$2" | tr ' ' '\n' | grep -qx "$GROUP"; then
        usermod -a -G "$GROUP" "$2"
    fi
    printf 'Enrolled %s in %s; log out and back in before using the socket.\n' "$2" "$GROUP"
    exit 0
fi

for command in dkms make python3 install modprobe mktemp stat cc systemctl getent groupadd groupdel sha256sum cut find; do
    command -v "$command" >/dev/null 2>&1 || {
        printf 'missing required command: %s\n' "$command" >&2
        exit 1
    }
done
if [ -d /sys/module/omen_rgb ]; then
    printf '%s\n' 'omen_rgb is already loaded; unload it before installing' >&2
    exit 1
fi
SELF_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
SOURCE=$(CDPATH= cd -- "${SELF_DIR}/.." && pwd -P)
BROKER_SOURCE="$SOURCE/broker/main.c"
MIGRATION_SOURCE="$SOURCE/scripts/export-profiles.py"
# The checkout is read as root. Reject links in every input and in the zipapp
# tree, not just the broker C file (zipapp traverses src recursively).
for directory in "$SOURCE/module" "$SOURCE/broker" "$SOURCE/packaging" "$SOURCE/src"; do
    if [ -L "$directory" ] || [ ! -d "$directory" ]; then
        printf 'unsafe source directory: %s\n' "$directory" >&2; exit 1
    fi
done
for path in "$SOURCE/Makefile" "$SOURCE/dkms.conf" \
            "$SOURCE/module/omen_rgb.c" "$SOURCE/module/omen_rgb_protocol.h" \
            "$SOURCE/omen-rgb.modules-load.conf" "$BROKER_SOURCE" \
            "$MIGRATION_SOURCE" \
            "$SOURCE/packaging/okeylitctl-broker.service" \
            "$SOURCE/packaging/okeylitctl-broker.socket"; do
    if [ -L "$path" ] || [ ! -f "$path" ]; then
        printf 'missing or unsafe source: %s\n' "$path" >&2; exit 1
    fi
done
if [ -n "$(find "$SOURCE/src" -type l -print -quit)" ]; then
    printf '%s\n' 'unsafe symlink in CLI source tree' >&2; exit 1
fi

# Refuse all preexisting install artifacts BEFORE creating anything.
for path in "$DEST" "$CLI_DEST" "$LEGACY_CLI_DEST" "$LOAD_DEST" \
            "$BROKER_DEST" "$MIGRATION_DEST" "$SERVICE_DEST" "$SOCKET_DEST" /run/okeylitctl.sock; do
    if [ -e "$path" ] || [ -L "$path" ]; then
        printf 'refusing existing installed path: %s\n' "$path" >&2
        printf '%s\n' 'remove the existing version deliberately before reinstalling' >&2
        exit 1
    fi
done
if [ -L "$BROKER_DIR" ] || { [ -e "$BROKER_DIR" ] && [ ! -d "$BROKER_DIR" ]; }; then
    printf 'refusing unsafe broker directory: %s\n' "$BROKER_DIR" >&2
    exit 1
fi
if [ -d "$BROKER_DIR" ]; then
    if [ "$(stat -c %u -- "$BROKER_DIR")" != 0 ] ||
       [ "$(stat -c %a -- "$BROKER_DIR")" != 755 ]; then
        printf 'refusing unsafe broker directory ownership or mode: %s\n' "$BROKER_DIR" >&2
        exit 1
    fi
fi
if [ -L "$PROFILE_DIR" ] || { [ -e "$PROFILE_DIR" ] && [ ! -d "$PROFILE_DIR" ]; }; then
    printf 'refusing unsafe profile path: %s\n' "$PROFILE_DIR" >&2
    exit 1
fi
if [ -d "$PROFILE_DIR" ]; then
    if [ "$(stat -c %u -- "$PROFILE_DIR")" != 0 ] ||
       [ "$(stat -c %g -- "$PROFILE_DIR")" != 0 ] ||
       [ "$(stat -c %a -- "$PROFILE_DIR")" != 700 ]; then
        printf 'refusing unsafe profile directory ownership or mode: %s\n' "$PROFILE_DIR" >&2
        exit 1
    fi
fi
# DKMS may have an entry even when its source directory was removed.
if [ -n "$(dkms status -m "$MODULE" -v "$DRIVER_VERSION")" ]; then
    printf '%s\n' 'refusing existing DKMS installation' >&2
    exit 1
fi
if getent group "$GROUP" >/dev/null && [ "${ACCEPT_EXISTING_GROUP:-0}" -ne 1 ]; then
    printf '%s\n' 'existing okeylitctl group could already contain authorized users; inspect its membership and rerun with --accept-existing-group only if approved' >&2
    exit 1
fi

TMP=""
BROKER_TMP=""
COMPLETE=0
CREATED_DEST=0
DKMS_ADDED=0
INSTALLED_CLI=0
INSTALLED_LOAD=0
INSTALLED_BROKER=0
INSTALLED_MIGRATION=0
INSTALLED_SERVICE=0
INSTALLED_SOCKET=0
STARTED_SOCKET=0
MAY_HAVE_LOADED=0
CREATED_PROFILE_DIR=0
CREATED_BROKER_DIR=0
CREATED_GROUP=0
cleanup()
{
    code=$?
    trap - 0 HUP INT TERM
    [ -z "$TMP" ] || rm -f -- "$TMP"
    [ -z "$BROKER_TMP" ] || rm -f -- "$BROKER_TMP"
    if [ "$COMPLETE" -ne 1 ]; then
        if [ "$STARTED_SOCKET" -eq 1 ]; then
            if ! systemctl disable --now okeylitctl-broker.socket ||
               ! systemctl stop okeylitctl-broker.service; then
                printf '%s\n' 'broker stop failed; keeping installed artifacts for safe manual recovery' >&2
                exit 1
            fi
        fi
        if [ "$MAY_HAVE_LOADED" -eq 1 ]; then
            modprobe -r omen_rgb >/dev/null 2>&1 || true
            if [ -d /sys/module/omen_rgb ]; then
                printf '%s\n' 'module unload failed; keeping DKMS, source and installed artifacts for safe manual recovery' >&2
                exit 1
            fi
        fi
        [ "$INSTALLED_SOCKET" -ne 1 ] || rm -f -- "$SOCKET_DEST"
        [ "$INSTALLED_SERVICE" -ne 1 ] || rm -f -- "$SERVICE_DEST"
        if [ "$INSTALLED_SOCKET" -eq 1 ] || [ "$INSTALLED_SERVICE" -eq 1 ]; then
            systemctl daemon-reload >/dev/null 2>&1 || true
        fi
        [ "$INSTALLED_LOAD" -ne 1 ] || rm -f -- "$LOAD_DEST"
        [ "$INSTALLED_CLI" -ne 1 ] || rm -f -- "$CLI_DEST"
        [ "$INSTALLED_BROKER" -ne 1 ] || rm -f -- "$BROKER_DEST"
        [ "$INSTALLED_MIGRATION" -ne 1 ] || rm -f -- "$MIGRATION_DEST"
        if [ "$CREATED_BROKER_DIR" -eq 1 ] && [ -d "$BROKER_DIR" ] && [ ! -L "$BROKER_DIR" ]; then
            rmdir -- "$BROKER_DIR" 2>/dev/null || true
        fi
        if [ "$CREATED_PROFILE_DIR" -eq 1 ] && [ -d "$PROFILE_DIR" ] && [ ! -L "$PROFILE_DIR" ]; then
            rmdir -- "$PROFILE_DIR" 2>/dev/null || true
        fi
        if [ "$DKMS_ADDED" -eq 1 ]; then
            dkms remove -m "$MODULE" -v "$DRIVER_VERSION" --all >/dev/null 2>&1 || true
        fi
        if [ "$CREATED_DEST" -eq 1 ] && [ -d "$DEST" ] && [ ! -L "$DEST" ]; then
            rm -rf -- "$DEST"
        fi
        if [ "$CREATED_GROUP" -eq 1 ]; then
            groupdel "$GROUP" >/dev/null 2>&1 || true
        fi
    fi
    exit "$code"
}
trap cleanup 0
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

if ! getent group "$GROUP" >/dev/null; then
    CREATED_GROUP=1
    groupadd --system "$GROUP"
fi
if [ ! -d "$PROFILE_DIR" ]; then
    CREATED_PROFILE_DIR=1
    install -d -o root -g root -m 0700 "$PROFILE_DIR"
fi
if [ ! -d "$BROKER_DIR" ]; then
    CREATED_BROKER_DIR=1
    install -d -o root -g root -m 0755 "$BROKER_DIR"
fi
CREATED_DEST=1
install -d -m 0755 "$DEST/module"
install -m 0644 "$SOURCE/Makefile" "$SOURCE/dkms.conf" "$DEST/"
install -m 0644 "$SOURCE/module/omen_rgb.c" \
    "$SOURCE/module/omen_rgb_protocol.h" "$DEST/module/"
chown -R root:root "$DEST"
chmod -R go-w "$DEST"

# Mark a partially successful DKMS add for rollback too.
DKMS_ADDED=1
dkms add -m "$MODULE" -v "$DRIVER_VERSION"
dkms build -m "$MODULE" -v "$DRIVER_VERSION"
dkms install -m "$MODULE" -v "$DRIVER_VERSION"

TMP=$(mktemp /tmp/okeylitctl.XXXXXX)
python3 -I -m zipapp "$SOURCE/src" -m okeylitctl.cli:entrypoint \
    -p /usr/bin/python3 -o "$TMP"
BROKER_TMP=$(mktemp /tmp/okeylitctl-broker.XXXXXX)
cc -std=c11 -O2 -Wall -Wextra -Werror "$BROKER_SOURCE" -o "$BROKER_TMP"
INSTALLED_CLI=1
install -m 0755 "$TMP" "$CLI_DEST"
INSTALLED_BROKER=1
install -m 0755 "$BROKER_TMP" "$BROKER_DEST"
INSTALLED_MIGRATION=1
install -o root -g root -m 0755 "$MIGRATION_SOURCE" "$MIGRATION_DEST"
INSTALLED_LOAD=1
install -m 0644 "$SOURCE/omen-rgb.modules-load.conf" "$LOAD_DEST"
INSTALLED_SERVICE=1
install -m 0644 "$SOURCE/packaging/okeylitctl-broker.service" "$SERVICE_DEST"
INSTALLED_SOCKET=1
install -m 0644 "$SOURCE/packaging/okeylitctl-broker.socket" "$SOCKET_DEST"
# The private manifest ties removal to these exact installed bytes. Its fixed
# ordering is shared with the uninstaller; snapshot files are included too.
{
    printf '%s\n' 'okeylitctl-manifest-v2'
    for path in "$DEST/Makefile" "$DEST/dkms.conf" \
                "$DEST/module/omen_rgb.c" "$DEST/module/omen_rgb_protocol.h" \
                "$CLI_DEST" "$LOAD_DEST" "$BROKER_DEST" "$SERVICE_DEST" "$SOCKET_DEST" \
                "$MIGRATION_DEST"; do
        sha256sum -- "$path" | cut -d ' ' -f 1
    done
} > "$MANIFEST"
chmod 0600 "$MANIFEST"
systemctl daemon-reload
MAY_HAVE_LOADED=1
modprobe omen_rgb
STARTED_SOCKET=1
systemctl enable --now okeylitctl-broker.socket
COMPLETE=1
printf '%s\n' "OKeyLitCtl ${APP_VERSION} installed; omen_rgb ${DRIVER_VERSION} loaded; enroll users separately with --allow-user USER"
