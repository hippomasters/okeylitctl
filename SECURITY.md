# Security policy

## Supported versions

Only the latest tagged release receives security fixes.

## Reporting a vulnerability

Do not publish firmware-safety or privilege-boundary vulnerabilities in a public issue. Use GitHub's private vulnerability reporting feature for this repository. If that feature is unavailable, open a minimal issue asking the maintainer for a private contact method without including exploit details.

Include the affected version, kernel version, exact DMI information, BIOS version, reproduction steps, and whether any firmware command was issued.

## Security model

- The module refuses every machine except explicitly allowlisted DMI and BIOS tuples.
- No force-load option or raw WMI command interface exists.
- Firmware writes are serialized, rate-limited, bounded, read-modify-write operations.
- Color writes preserve all firmware-table bytes outside offsets 25–36.
- Direct sysfs mutation is root-only. A root-owned, socket-activated native broker accepts bounded fixed operations on `/run/okeylitctl.sock` (`SOCK_SEQPACKET`, `root:okeylitctl`, mode `0660`); users are enrolled only with a separate explicit `--allow-user USER` invocation and must log in again. The CLI never invokes `sudo` and is never installed setuid.
- The service uses `NoNewPrivileges`, an empty capability bounding set, and a filesystem policy that keeps the device sysfs ABI writable; protection that remounts sysfs read-only would break its required write path. Installation/uninstallation require root. User profiles belong in the enrolled user's private data directory; legacy root-owned profiles under `/var/lib/okeylitctl` are preserved on uninstall.
- The installer must not download code or register a user-writable checkout with DKMS.
- Legacy profile export is an explicit root action using only the installed, root-owned `/usr/local/libexec/okeylitctl-export-profiles` and `/usr/local/bin/okeylitctl` zipapp. Run `/usr/bin/python3 -I` on the installed helper; never elevate a migration script in a user-writable checkout. The helper refuses symlinked, non-root-owned or group/world-writable ancestors of its elevated executable and imported zipapp. The installed helper is covered by the installer manifest and rollback and is removed on uninstall; legacy profiles remain untouched.

This is an experimental driver for an undocumented firmware interface. An allowlisted model can still vary after a firmware update. Unsupported hardware must be added only after a read-only probe and reviewed hardware report.

The module cannot share its transaction mutex with the in-tree `hp_wmi` driver, which may invoke the same BIOS GUID for unrelated HP controls. Avoid simultaneous firmware-control operations. This limitation is one reason support remains restricted to the exact validated hardware and BIOS tuple.
