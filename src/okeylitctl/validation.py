"""Validation helpers for the OKeyLitCtl command."""

from __future__ import annotations

import re

_COLOR_PATTERN = re.compile(r"[0-9A-Fa-f]{6}(?:,[0-9A-Fa-f]{6}){3}", re.ASCII)


class ValidationError(ValueError):
    """Raised when a user value does not match the public ABI."""


def normalize_colors(value: str) -> str:
    """Validate and canonicalize four comma-separated RRGGBB values."""
    if not isinstance(value, str) or _COLOR_PATTERN.fullmatch(value) is None:
        raise ValidationError(
            "colors must be exactly four comma-separated RRGGBB values"
        )
    return value.upper()
