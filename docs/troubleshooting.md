# Troubleshooting

## Module refuses to load

Inspect kernel messages:

```bash
sudo journalctl -k -b | grep omen_rgb
```

A refusal on a different SKU, board, or BIOS is intentional. Do not patch out the allowlist. Submit a read-only hardware report instead.

## Secure Boot

Check the current state with your distribution's supported tool. If signature enforcement is active, configure DKMS to use a locally protected signing key, enroll only the public certificate through your distribution's MOK or platform-key workflow, rebuild the module, and verify its signer:

```bash
modinfo -F signer omen_rgb
```

Never publish or commit a private signing key. Never strip or modify a module after it is signed.

## Module missing after a kernel update

Confirm matching headers are installed and inspect:

```bash
dkms status
sudo dkms autoinstall
```

## Firmware operation returns an error

Stop repeated writes, restore if possible, and record the DMI, BIOS, kernel log, and exact command. The driver intentionally performs no automatic retry loop.

## Turning the keyboard lighting on or off

Use the laptop's physical keyboard-light key. The `state` ABI is read-only: software power writes were excluded after they failed repeat-cycle validation on the supported firmware.
