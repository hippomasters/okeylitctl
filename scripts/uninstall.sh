#!/bin/sh
set -eu

PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
unset PYTHONPATH PYTHONHOME PYTHONSTARTUP PYTHONINSPECT

VERSION="0.1.0"
MODULE="omen-rgb"
DEST="/usr/src/${MODULE}-${VERSION}"

if [ "$(id -u)" -ne 0 ]; then
    printf '%s\n' "uninstall.sh must be run as root" >&2
    exit 1
fi
for command in dkms modprobe; do
    command -v "$command" >/dev/null 2>&1 || {
        printf 'missing required command: %s\n' "$command" >&2
        exit 1
    }
done

if [ -d /sys/module/omen_rgb ]; then
    modprobe -r omen_rgb
fi

status=$(dkms status -m "$MODULE" -v "$VERSION")
if [ -n "$status" ]; then
    dkms remove -m "$MODULE" -v "$VERSION" --all
fi
status=$(dkms status -m "$MODULE" -v "$VERSION")
if [ -n "$status" ]; then
    printf '%s\n' "DKMS still reports omen-rgb; refusing partial cleanup" >&2
    exit 1
fi

rm -f -- /etc/modules-load.d/omen-rgb.conf /usr/local/bin/omen-rgb
if [ -L "$DEST" ]; then
    printf 'refusing symlink source directory: %s\n' "$DEST" >&2
    exit 1
fi
if [ -d "$DEST" ]; then
    rm -rf -- "$DEST"
fi

if [ -d /sys/module/omen_rgb ]; then
    printf '%s\n' "omen_rgb is still loaded; uninstall incomplete" >&2
    exit 1
fi
printf '%s\n' "omen-rgb ${VERSION} removed"
