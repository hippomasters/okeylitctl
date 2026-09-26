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
- Sysfs mutation is root-only. The CLI never invokes `sudo` and is never installed setuid.
- The installer must not download code or register a user-writable checkout with DKMS.

This is an experimental driver for an undocumented firmware interface. An allowlisted model can still vary after a firmware update. Unsupported hardware must be added only after a read-only probe and reviewed hardware report.

The module cannot share its transaction mutex with the in-tree `hp_wmi` driver, which may invoke the same BIOS GUID for unrelated HP controls. Avoid simultaneous firmware-control operations. This limitation is one reason support remains restricted to the exact validated hardware and BIOS tuple.
