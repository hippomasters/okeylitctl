# Kernel/userspace ABI

ABI version: `1`

Device directory:

```text
/sys/bus/platform/devices/omen-rgb/
```

| Attribute | Mode | Format |
|---|---:|---|
| `abi_version` | `0444` | `1` |
| `colors` | `0600` | Four comma-separated `RRGGBB` values |
| `original` | `0444` | Colors captured at module load |
| `state` | `0400` | Read-only `on` or `off` |
| `restore` | `0200` | Write exactly `1` |

Live firmware-backed reads and every mutation are root-only, preventing unprivileged callers from flooding firmware requests. Cached `original` and static `abi_version` remain public. Firmware state writes are intentionally absent because they did not pass repeat-cycle hardware validation. There is intentionally no raw command, offset, buffer, ioctl, procfs, or debugfs interface.

Zone values follow HP firmware table order. Confirm physical zone order when adding a newly supported machine.
