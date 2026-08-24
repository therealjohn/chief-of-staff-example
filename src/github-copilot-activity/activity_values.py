"""Normalize Microsoft 365 Agents SDK values at the Activity seam."""

from __future__ import annotations

from typing import Any


def as_dict(value: Any) -> dict[str, Any]:
    """Return an SDK mapping or an empty mapping for unsupported values."""
    if isinstance(value, dict):
        return value
    dump = getattr(value, "model_dump", None)
    if not callable(dump):
        return {}
    dumped = dump(by_alias=True)
    return dumped if isinstance(dumped, dict) else {}
