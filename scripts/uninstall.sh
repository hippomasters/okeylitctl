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
BROKER_DEST="/usr/local/libexec/okeylitctl-broker"
MIGRATION_DEST="/usr/local/libexec/okeylitctl-export-profiles"
SERVICE_DEST="/etc/systemd/system/okeylitctl-broker.service"
SOCKET_DEST="/etc/systemd/system/okeylitctl-broker.socket"
LOAD_DEST="/etc/modules-load.d/omen-rgb.conf"
CLI_DEST="/usr/local/bin/okeylitctl"
LEGACY_CLI_DEST="/usr/local/bin/omen-rgb"
PROFILE_DIR="/var/lib/okeylitctl"
MANIFEST="$DEST/.okeylitctl-manifest"

if [ "$(id -u)" -ne 0 ]; then
    printf '%s\n' 'uninstall.sh must be run as root' >&2
    exit 1
fi
for command in dkms modprobe systemctl stat sha256sum cut python3; do
    command -v "$command" >/dev/null 2>&1 || {
        printf 'missing required command: %s\n' "$command" >&2
        exit 1
    }
done
if [ -L "$DEST" ]; then
    printf 'refusing symlink source directory: %s\n' "$DEST" >&2
    exit 1
fi
if [ -d "$DEST" ]; then
    if [ "$(stat -c %u -- "$DEST")" != 0 ] ||
       [ "$(stat -c %a -- "$DEST")" != 755 ]; then
        printf 'refusing unsafe source directory: %s\n' "$DEST" >&2
        exit 1
    fi
fi
if [ -e "$DEST" ] && [ ! -d "$DEST" ]; then
    printf 'refusing unsafe source path: %s\n' "$DEST" >&2
    exit 1
fi
# A snapshot is removable only if its complete, expected structure is present.
# Do not recursively delete extra files that DKMS did not install.
if [ -d "$DEST" ]; then
    if [ -L "$DEST/module" ] || [ ! -d "$DEST/module" ] ||
       [ "$(stat -c %u:%a -- "$DEST/module")" != 0:755 ]; then
        printf '%s\n' 'refusing unsafe module snapshot directory' >&2; exit 1
    fi
    for path in "$DEST"/* "$DEST"/.[!.]* "$DEST"/..?* \
                "$DEST"/module/* "$DEST"/module/.[!.]* "$DEST"/module/..?*; do
        [ -e "$path" ] || [ -L "$path" ] || continue
        case "$path" in
            "$DEST/Makefile"|"$DEST/dkms.conf"|"$DEST/module"|\
            "$DEST/module/omen_rgb.c"|"$DEST/module/omen_rgb_protocol.h"|"$MANIFEST") ;;
            *) printf 'refusing unexpected snapshot entry: %s\n' "$path" >&2; exit 1 ;;
        esac
    done
fi
# Never delete arbitrary files installed at our expected paths.
for path in "$CLI_DEST" "$LEGACY_CLI_DEST" "$LOAD_DEST" "$BROKER_DEST" "$MIGRATION_DEST" \
            "$SERVICE_DEST" "$SOCKET_DEST"; do
    case "$path" in
        "$CLI_DEST"|"$LEGACY_CLI_DEST"|"$BROKER_DEST"|"$MIGRATION_DEST") expected_mode=755 ;;
        *) expected_mode=644 ;;
    esac
    if [ -e "$path" ] || [ -L "$path" ]; then
        if [ -L "$path" ] || [ ! -f "$path" ] ||
           [ "$(stat -c %u -- "$path")" != 0 ] ||
           [ "$(stat -c %a -- "$path")" != "$expected_mode" ]; then
            printf 'refusing unsafe installed path: %s\n' "$path" >&2
            exit 1
        fi
    fi
done
# New installs carry a root-only digest manifest; old 0.2.0 installs do not.
# Legacy files are accepted only with a recognizable DKMS snapshot and zipapp.
if [ -e "$MANIFEST" ] || [ -L "$MANIFEST" ]; then
    if [ -L "$MANIFEST" ] || [ ! -f "$MANIFEST" ] ||
       [ "$(stat -c %u:%a -- "$MANIFEST")" != 0:600 ] ||
       [ ! -d "$DEST" ]; then
        printf '%s\n' 'refusing unsafe installation manifest' >&2; exit 1
    fi
    exec 3< "$MANIFEST"
    IFS= read -r header <&3 || { printf '%s\n' 'incomplete manifest' >&2; exit 1; }
    case "$header" in
        okeylitctl-manifest-v1|okeylitctl-manifest-v2) ;;
        *) printf '%s\n' 'unknown manifest' >&2; exit 1 ;;
    esac
    for path in "$DEST/Makefile" "$DEST/dkms.conf" \
                "$DEST/module/omen_rgb.c" "$DEST/module/omen_rgb_protocol.h" \
                "$CLI_DEST" "$LOAD_DEST" "$BROKER_DEST" "$SERVICE_DEST" "$SOCKET_DEST"; do
        IFS= read -r expected <&3 || { printf '%s\n' 'incomplete manifest' >&2; exit 1; }
        if [ -L "$path" ] || [ ! -f "$path" ] ||
           [ "$(stat -c %u -- "$path")" != 0 ] ||
           [ "$(sha256sum -- "$path" | cut -d ' ' -f 1)" != "$expected" ]; then
            printf 'refusing modified or missing installed file: %s\n' "$path" >&2; exit 1
        fi
    done
    if [ "$header" = okeylitctl-manifest-v2 ]; then
        IFS= read -r expected <&3 || { printf '%s\n' 'incomplete manifest' >&2; exit 1; }
        if [ -L "$MIGRATION_DEST" ] || [ ! -f "$MIGRATION_DEST" ] ||
           [ "$(stat -c %u:%a -- "$MIGRATION_DEST")" != 0:755 ] ||
           [ "$(sha256sum -- "$MIGRATION_DEST" | cut -d ' ' -f 1)" != "$expected" ]; then
            printf 'refusing modified or missing installed file: %s\n' "$MIGRATION_DEST" >&2; exit 1
        fi
    elif [ -e "$MIGRATION_DEST" ] || [ -L "$MIGRATION_DEST" ]; then
        printf 'refusing unmanifested migration helper: %s\n' "$MIGRATION_DEST" >&2; exit 1
    fi
    if IFS= read -r excess <&3; then
        printf '%s\n' 'refusing unexpected manifest data' >&2; exit 1
    fi
    exec 3<&-
    if [ -e "$LEGACY_CLI_DEST" ] || [ -L "$LEGACY_CLI_DEST" ]; then
        printf '%s\n' 'refusing unmanifested legacy command' >&2; exit 1
    fi
else
    # A partial or unrelated installation without proof of ownership must be
    # inspected manually. A legacy release has no broker units or binary.
    for path in "$BROKER_DEST" "$MIGRATION_DEST" "$SERVICE_DEST" "$SOCKET_DEST"; do
        if [ -e "$path" ] || [ -L "$path" ]; then
            printf 'refusing unmanifested broker artifact: %s\n' "$path" >&2; exit 1
        fi
    done
    if [ ! -d "$DEST" ]; then
        for path in "$CLI_DEST" "$LEGACY_CLI_DEST" "$LOAD_DEST"; do
            if [ -e "$path" ] || [ -L "$path" ]; then
                printf 'refusing unowned artifact without a DKMS snapshot: %s\n' "$path" >&2; exit 1
            fi
        done
    fi
    if [ -d "$DEST" ]; then
        # Pin the actual pre-manifest 0.2.0 snapshot, not the current checkout:
        # the build Makefile changed after that release.
        for name in Makefile dkms.conf module/omen_rgb.c module/omen_rgb_protocol.h; do
            case "$name" in
                Makefile) expected=b45ef8507dcbae8e5447cc0495ce09113733e29d45877d8cc653f6eb480fc3da ;;
                dkms.conf) expected=e94c2f2dfb1aa2f30175ae5ab29e7afd41824fb50dc188b394fd99696a0e5b76 ;;
                module/omen_rgb.c) expected=ff6e262c54833710777a3deb9f6f3ee7c642f0889933173caac4eb714df73678 ;;
                module/omen_rgb_protocol.h) expected=c5514f6c60f812e1af7e49d6bf21de5844a18a4fcc6a860b703a2b145990e3c4 ;;
            esac
            path="$DEST/$name"
            if [ -L "$path" ] || [ ! -f "$path" ] ||
               [ "$(stat -c %u:%a -- "$path")" != 0:644 ] ||
               [ "$(sha256sum -- "$path" | cut -d ' ' -f 1)" != "$expected" ]; then
                printf 'refusing unrecognized legacy snapshot file: %s\n' "$path" >&2; exit 1
            fi
        done
    fi
    if [ -e "$LOAD_DEST" ] &&
       [ "$(sha256sum -- "$LOAD_DEST" | cut -d ' ' -f 1)" != \
         "$(printf 'omen_rgb\n' | sha256sum | cut -d ' ' -f 1)" ]; then
        printf '%s\n' 'refusing unrelated modules-load entry' >&2; exit 1
    fi
    for spec in "$CLI_DEST:okeylitctl:0.2.0" "$LEGACY_CLI_DEST:omen_rgb:0.1.0"; do
        path=${spec%%:*}
        spec=${spec#*:}
        package=${spec%%:*}
        version=${spec#*:}
        [ -e "$path" ] || continue
        if [ ! -d "$DEST" ] || ! python3 -I -c '
import sys, zipfile
path, package, version = sys.argv[1:]
try:
    with zipfile.ZipFile(path) as archive:
        main = archive.read("__main__.py")
        init = archive.read(package + "/__init__.py")
        cli = archive.read(package + "/cli.py")
        assert main == ("# -*- coding: utf-8 -*-\\nimport " + package + ".cli\\n" + package + ".cli.entrypoint()\\n").encode().replace(b"\\n", b"\n")
        assert ("__version__ = \"" + version + "\"").encode() in init
        assert (b"APP_NAME = \"okeylitctl\"" if package == "okeylitctl" else b"prog=\"omen-rgb\"") in cli
except (OSError, KeyError, zipfile.BadZipFile, AssertionError):
    sys.exit(1)
' "$path" "$package" "$version"; then
            printf 'refusing unrecognized legacy command: %s\n' "$path" >&2; exit 1
        fi
    done
fi
# Prevent new clients and finish an active broker before unloading the module.
if [ -e "$SOCKET_DEST" ]; then
    systemctl disable --now okeylitctl-broker.socket
    systemctl stop okeylitctl-broker.service
fi
if [ -d /sys/module/omen_rgb ]; then
    modprobe -r omen_rgb
fi
status=$(dkms status -m "$MODULE" -v "$DRIVER_VERSION")
if [ -n "$status" ]; then
    dkms remove -m "$MODULE" -v "$DRIVER_VERSION" --all
fi
status=$(dkms status -m "$MODULE" -v "$DRIVER_VERSION")
if [ -n "$status" ]; then
    printf '%s\n' 'DKMS still reports omen-rgb; refusing partial cleanup' >&2
    exit 1
fi
if [ -d /sys/module/omen_rgb ]; then
    printf '%s\n' 'omen_rgb is still loaded; uninstall incomplete' >&2
    exit 1
fi
rm -f -- "$SOCKET_DEST" "$SERVICE_DEST" "$BROKER_DEST" "$MIGRATION_DEST" \
    "$LOAD_DEST" "$CLI_DEST" "$LEGACY_CLI_DEST"
systemctl daemon-reload
if [ -d "$DEST" ]; then
    rm -rf -- "$DEST"
fi
# Preserve root-owned profiles and the local group/membership intentionally.
printf '%s\n' "OKeyLitCtl ${APP_VERSION} and omen-rgb ${DRIVER_VERSION} removed; preserved ${PROFILE_DIR} and group membership"
