"""Tests for generic Invocations payload validation.

Design ref (item 6): the generic notification fan-out (item 2) is triggered
by an Invocations payload that must contain a non-empty ``notification``
string, and reject:

  * a missing ``notification`` key
  * a blank / whitespace-only ``notification`` value
  * a non-string ``notification`` value
  * unsupported/extra shapes (unrecognized additional top-level keys) --
    there is no daily-briefing-shaped payload variant in this design

Expected (not yet implemented) production module: ``invocations``
  - ``NotificationInvocationPayload`` -- a strict schema (e.g. a pydantic
    model with ``extra="forbid"``) exposing a validated, stripped
    ``notification: str`` field, raising on any of the invalid shapes above.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from invocations import NotificationInvocationPayload  # type: ignore[import-not-found]


def test_accepts_payload_with_a_nonempty_notification_string():
    payload = NotificationInvocationPayload.model_validate({"notification": "Your report is ready."})

    assert payload.notification == "Your report is ready."


def test_rejects_payload_missing_the_notification_key():
    with pytest.raises(ValidationError):
        NotificationInvocationPayload.model_validate({})


def test_rejects_blank_notification_string():
    with pytest.raises(ValidationError):
        NotificationInvocationPayload.model_validate({"notification": ""})


def test_rejects_whitespace_only_notification_string():
    with pytest.raises(ValidationError):
        NotificationInvocationPayload.model_validate({"notification": "   "})


def test_rejects_non_string_notification_value():
    with pytest.raises(ValidationError):
        NotificationInvocationPayload.model_validate({"notification": 12345})


def test_rejects_payload_with_unsupported_extra_keys():
    with pytest.raises(ValidationError):
        NotificationInvocationPayload.model_validate({
            "notification": "hello",
            "briefingType": "daily",  # no daily-briefing shape belongs here
        })
