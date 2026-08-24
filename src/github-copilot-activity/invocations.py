"""Generic Invocations payload validation.

The generic notification fan-out is triggered by an Invocations payload that
must contain a non-empty ``notification`` string, and rejects any other
top-level shape -- there is no daily-briefing-shaped payload variant here.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator


class NotificationInvocationPayload(BaseModel):
    """Strict schema for a generic notification Invocations payload."""

    model_config = ConfigDict(extra="forbid")

    notification: str

    @field_validator("notification")
    @classmethod
    def _notification_must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("notification must not be blank")
        return stripped
