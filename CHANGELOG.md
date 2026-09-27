# Changelog

## 0.2.0 — 2026-09-27

- Renamed the public project, Python package, and CLI to OKeyLitCtl / `okeylitctl`.
- Retained the internal `omen_rgb` kernel module, `omen-rgb` DKMS package, and sysfs path for installed-driver compatibility.
- The new uninstaller removes both `okeylitctl` and the legacy `omen-rgb` command during migration.
- The internal driver remains version 0.1.0 because no kernel or firmware protocol behavior changed.

## 0.1.0 — 2026-09-26

- Initial hardware-validated release for HP OMEN 16-wf0xxx SKU B21E3PA#ACJ, board 8BAB, BIOS F.26.
- Four-zone RGB read, write, verification, restore, and read-only backlight-state reporting.
- Software backlight power writes excluded after repeat-cycle hardware validation failed closed.
- Fail-closed DMI, keyboard-type, firmware-response, table-marker, and ABI checks.
- Root-only mutation through fixed sysfs attributes.
- Standard-library Python CLI with no daemon, setuid helper, network access, or automatic privilege escalation.
