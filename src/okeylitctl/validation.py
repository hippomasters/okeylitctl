"""Validation helpers for the OKeyLitCtl command."""

from __future__ import annotations

import re

_COLOR_PATTERN = re.compile(r"[0-9A-Fa-f]{6}", re.ASCII)


class ValidationError(ValueError):
    """Raised when a user value does not match the public ABI."""


def normalize_color(value: str) -> str:
    """Validate and canonicalize one RRGGBB value."""
    if not isinstance(value, str) or _COLOR_PATTERN.fullmatch(value) is None:
        raise ValidationError("color must be exactly one RRGGBB value")
    return value.upper()


def normalize_colors(value: str) -> str:
    """Validate and canonicalize four comma-separated RRGGBB values."""
    if not isinstance(value, str):
        raise ValidationError(
            "colors must be exactly four comma-separated RRGGBB values"
        )
    values = value.split(",")
    if len(values) != 4:
        raise ValidationError(
            "colors must be exactly four comma-separated RRGGBB values"
        )
    try:
        return ",".join(normalize_color(color) for color in values)
    except ValidationError:
        raise ValidationError(
            "colors must be exactly four comma-separated RRGGBB values"
        ) from None
