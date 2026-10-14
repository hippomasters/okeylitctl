# OKeyLitCtl

A restricted Linux controller for HP OMEN four-zone keyboard RGB lighting through HP's firmware WMI interface. The public command is `okeylitctl`; the internal kernel module `omen_rgb`, DKMS package `omen-rgb`, and sysfs path remain stable for compatibility.

> **Experimental firmware driver:** OKeyLitCtl 0.2.0 bundles internal driver 0.1.0, which intentionally supports one hardware/BIOS tuple. It refuses everything else. Do not remove the checks to force unsupported hardware.

## Confirmed hardware

| Field | Supported value |
|---|---|
| Product | `OMEN by HP Gaming Laptop 16-wf0xxx` |
| SKU | `B21E3PA#ACJ` |
| Board | `8BAB` |
| BIOS | `F.26` |
| Keyboard type | HP WMI `0x02` — four zones, with the numpad included in the Right zone |
| Tested kernel | Arch Linux `7.2.6-arch2-1` |

Hardware testing confirmed RGB read/write/readback, restore, and read-only power-state reporting on September 26, 2026. Software power writes are intentionally not exposed because repeated off/on cycles were not reliable on the validated firmware; use the laptop's physical keyboard-light key for power control.

## Hardware-verified zone order

The four values passed to `okeylitctl colors` use firmware order, not physical left-to-right order:

| Value position | HP lighting-zone name | Physical area |
|---:|---|---|
| 1 | Right zone | Far-right section, including Backspace, Enter, and the numpad |
| 2 | Center zone | Center section |
| 3 | Left zone | Left section outside the dedicated WASD group |
| 4 | WASD zone | W, A, S, D area |

For example, `FFFFFF,000000,000000,000000` lights only the right zone. This mapping was physically verified on the supported laptop. HP's OMEN lighting software uses the corresponding Right, Center, Left, and WASD zone terminology.

## Safety properties

- Exact DMI, BIOS, WMI GUID, keyboard-type, response-length, and table-marker checks.
- No raw WMI commands, arbitrary payloads, debugfs, ioctl, network access, setuid helper, or force-unsupported switch.
- A root-owned socket-activated C broker accepts only bounded fixed operations over a local `SOCK_SEQPACKET` socket; no dynamic filesystem paths or client-provided profile paths.
- Every color update begins with a validated 128-byte GET and changes only RGB offsets 25–36.
- Firmware calls are serialized and writes are rate-limited.
- Direct sysfs mutation remains root-only; enrolled members of the `okeylitctl` group may request the broker's narrow lighting operations.
- The client validates layouts and verifies readback; the broker alone accesses fixed allowlisted sysfs paths.
- DKMS installs a root-owned source snapshot rather than registering the writable checkout.

Read [SECURITY.md](SECURITY.md) before adding hardware support.

## Known limitation

This experimental out-of-tree module and Linux's `hp_wmi` driver can both call the same HP BIOS WMI GUID. Their internal locks are not shared. No conflict was observed on the validated laptop, but avoid running other HP firmware-control operations while changing keyboard lighting. A future upstream-quality implementation should integrate this support into `hp-wmi` rather than remain a second WMI consumer.

## Commands

```console
$ okeylitctl status
State:    on
Colors:   FF0000,00FF00,0000FF,FFFFFF
Original: 580BC3,D00FEF,4D0998,AF0AA1

$ okeylitctl status --json
$ okeylitctl colors FF0000,00FF00,0000FF,FFFFFF
$ okeylitctl restore
```

After explicit enrollment and a fresh login, the CLI and TUI run as the user, without `sudo`. `status` reports the firmware's current `on`/`off` state but does not change it. The output above illustrates formatting; the new broker path has not been validated on hardware.

## Install with DKMS

Install matching kernel headers, DKMS, a compiler toolchain, Python 3, and systemd. On Arch Linux (installation is privileged; ordinary app use is not):

```bash
sudo pacman -S --needed base-devel dkms linux-headers python
sudo ./scripts/install-dkms.sh
sudo ./scripts/install-dkms.sh --allow-user YOUR_LOGIN
```

Replace `YOUR_LOGIN` with an existing local user name, then **log out and back in** so the new `okeylitctl` group membership takes effect. Enrollment is never inferred from `SUDO_USER` and is not part of the default installation. Group members can request lighting changes on this machine; only enroll trusted users. The installer refuses to overwrite existing artifacts. Run this release's uninstaller before reinstalling/upgrading.

The installer performs no downloads. It copies a root-owned snapshot to `/usr/src/omen-rgb-0.1.0`, builds the stable internal `omen_rgb` driver through DKMS, builds and installs the C broker at `/usr/local/libexec/okeylitctl-broker`, installs systemd service/socket units (socket mode `0660`, group `okeylitctl`), a modules-load entry, a Python zipapp at `/usr/local/bin/okeylitctl`, and a manifest-tracked, root-owned one-time profile migration helper at `/usr/local/libexec/okeylitctl-export-profiles`, then loads the module and enables the socket. No setuid binary is installed. The uninstaller stops the broker before unloading and removes installed artifacts, including the migration helper and legacy `/usr/local/bin/omen-rgb` command; it preserves root-owned profile data and the group/memberships.

### One-time migration of old system-wide profiles

The 0.2.0 profiles under `/var/lib/okeylitctl/profiles.json` are **not** moved or deleted automatically. If you intend to grant their contents to your current account, install the new release first and, as that account, run this command in Bash:

```bash
sudo -v
set -o pipefail
sudo -n /usr/bin/python3 -I /usr/local/libexec/okeylitctl-export-profiles --export | /usr/bin/python3 -I /usr/local/libexec/okeylitctl-export-profiles --import
```

Only the left side runs as root. Both sides use the installed helper and root-owned installed zipapp, **not** a script or Python package from your checkout; never run `sudo python3 scripts/export-profiles.py`. The helper checks ownership, symlinks and writable ancestors before importing the zipapp. The legacy source remains untouched, the destination belongs to the invoking account and is never overwritten; if that account already has a profile store, the import fails rather than merges. Treat any saved export as private data.

Secure Boot systems must locally sign and trust the DKMS-built module. No private signing key is included. See [docs/troubleshooting.md](docs/troubleshooting.md).

## Manual development build

```bash
make
make test
sudo insmod ./omen_rgb.ko
```

Loading a development module alone does not start the broker; client commands require the installed socket/service or a separate isolated test setup.

The public ABI is documented in [docs/kernel-abi.md](docs/kernel-abi.md).

## Uninstall

```bash
sudo ./scripts/uninstall.sh
```

Unloading leaves the current keyboard state unchanged.

## Adding hardware

Open an issue with read-only probe results: complete DMI identifiers, BIOS version, keyboard type, kernel version, and color-table marker. New hardware requires a reviewed allowlist entry and confirmed restore test. There is intentionally no bypass flag.

## License and attribution

GPL-2.0-only. The HP WMI packet structure and keyboard-lighting protocol are based on the GPL-licensed Linux `hp-wmi` driver and its upstream keyboard-backlight patch series. See source comments and repository history for attribution.
