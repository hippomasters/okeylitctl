# OMEN RGB Linux

A restricted Linux kernel module and CLI for HP OMEN four-zone keyboard RGB control through HP's firmware WMI interface.

> **Experimental firmware driver:** version 0.1.0 intentionally supports one hardware/BIOS tuple. It refuses everything else. Do not remove the checks to force unsupported hardware.

## Confirmed hardware

| Field | Supported value |
|---|---|
| Product | `OMEN by HP Gaming Laptop 16-wf0xxx` |
| SKU | `B21E3PA#ACJ` |
| Board | `8BAB` |
| BIOS | `F.26` |
| Keyboard type | HP WMI `0x02` — four-zone without numpad |
| Tested kernel | Arch Linux `7.2.6-arch2-1` |

Hardware testing confirmed RGB read/write/readback, restore, and read-only power-state reporting on September 26, 2026. Software power writes are intentionally not exposed because repeated off/on cycles were not reliable on the validated firmware; use the laptop's physical keyboard-light key for power control.

## Safety properties

- Exact DMI, BIOS, WMI GUID, keyboard-type, response-length, and table-marker checks.
- No raw WMI commands, arbitrary payloads, debugfs, ioctl, daemon, network access, setuid helper, or force-unsupported switch.
- Every color update begins with a validated 128-byte GET and changes only RGB offsets 25–36.
- Firmware calls are serialized and writes are rate-limited.
- Live status and mutation are root-only; only cached original colors and the ABI version are public.
- The CLI uses fixed allowlisted sysfs paths, rejects symlinks and malformed output, performs one bounded write, and verifies readback.
- DKMS installs a root-owned source snapshot rather than registering the writable checkout.

Read [SECURITY.md](SECURITY.md) before adding hardware support.

## Known limitation

This experimental out-of-tree module and Linux's `hp_wmi` driver can both call the same HP BIOS WMI GUID. Their internal locks are not shared. No conflict was observed on the validated laptop, but avoid running other HP firmware-control operations while changing keyboard lighting. A future upstream-quality implementation should integrate this support into `hp-wmi` rather than remain a second WMI consumer.

## Commands

```console
$ sudo omen-rgb status
State:    on
Colors:   FF0000,00FF00,0000FF,FFFFFF
Original: 580BC3,D00FEF,4D0998,AF0AA1

$ sudo omen-rgb status --json
$ sudo omen-rgb colors FF0000,00FF00,0000FF,FFFFFF
$ sudo omen-rgb restore
```

The CLI never invokes `sudo`; mutation fails unless the caller already has root privileges. `status` reports the firmware's current `on`/`off` state but does not change it.

## Install with DKMS

Install matching kernel headers, DKMS, a compiler toolchain, and Python 3. On Arch Linux:

```bash
sudo pacman -S --needed base-devel dkms linux-headers python
sudo ./scripts/install-dkms.sh
```

The installer performs no downloads. It copies a root-owned snapshot to `/usr/src/omen-rgb-0.1.0`, builds through DKMS, installs a modules-load entry, creates a Python zipapp at `/usr/local/bin/omen-rgb`, and loads the module.

Secure Boot systems must locally sign and trust the DKMS-built module. No private signing key is included. See [docs/troubleshooting.md](docs/troubleshooting.md).

## Manual development build

```bash
make
make test
sudo insmod ./omen_rgb.ko
PYTHONPATH=src python -m omen_rgb status
```

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
