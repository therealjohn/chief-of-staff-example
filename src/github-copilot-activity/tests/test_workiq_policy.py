"""Tests for the Work IQ hard policy (fail-closed tool gate).

Design ref (item 5): a structured classifier maps concrete tool names (+
arguments) to an allow/deny decision, independent of any real connector:

  * ALL read operations are allowed
  * safe writes:
      - Outlook draft creation -> always allowed
      - Calendar event create  -> allowed ONLY when a transactionId is present
      - Calendar event update  -> allowed ONLY when If-Match/eTag info is present
      - OneDrive/SharePoint item update -> allowed ONLY as a conditional update
        to an EXISTING item (If-Match/eTag info present)
  * explicitly rejected, unconditionally:
      - send/reply mail, Teams posts, sharing, cancellations,
        file/folder create/copy, destructive deletes
  * unknown/unrecognized tool names fail CLOSED (denied)

Expected (not yet implemented) production module: ``workiq_policy``
  - ``PolicyDecision`` enum-like with ``ALLOW`` / ``DENY`` members
  - ``evaluate(tool_name: str, arguments: dict) -> PolicyResult`` where
    ``PolicyResult`` has ``.decision`` and a human-readable ``.reason``
"""

from __future__ import annotations

import pytest

from workiq_policy import PolicyDecision, evaluate  # type: ignore[import-not-found]

READ_TOOL_NAMES = [
    "mail_search",
    "mail_read",
    "calendar_read",
    "calendar_list",
    "onedrive_read",
    "sharepoint_read",
    "people_search",
    "teams_read_messages",
]

UNCONDITIONALLY_REJECTED_TOOL_NAMES = [
    "mail_send",
    "mail_reply",
    "mail_reply_all",
    "mail_forward",
    "teams_post_message",
    "teams_reply_message",
    "onedrive_item_share",
    "sharepoint_item_share",
    "calendar_event_cancel",
    "onedrive_folder_create",
    "onedrive_item_copy",
    "sharepoint_folder_create",
    "sharepoint_item_copy",
    "onedrive_item_delete",
    "sharepoint_item_delete",
    "calendar_event_delete",
    "mail_delete",
]


@pytest.mark.parametrize("tool_name", READ_TOOL_NAMES)
def test_read_operations_are_always_allowed(tool_name):
    result = evaluate(tool_name, {})
    assert result.decision is PolicyDecision.ALLOW


def test_outlook_draft_creation_is_always_allowed():
    result = evaluate("outlook_draft_create", {"subject": "hi", "body": "hello"})
    assert result.decision is PolicyDecision.ALLOW


def test_calendar_event_create_is_allowed_when_transaction_id_present():
    result = evaluate(
        "calendar_event_create",
        {"subject": "Sync", "transactionId": "8f14e45f-ceea-4b19"},
    )
    assert result.decision is PolicyDecision.ALLOW


def test_calendar_event_create_is_denied_when_transaction_id_missing():
    result = evaluate("calendar_event_create", {"subject": "Sync"})
    assert result.decision is PolicyDecision.DENY


def test_calendar_event_create_is_denied_when_transaction_id_blank():
    result = evaluate("calendar_event_create", {"subject": "Sync", "transactionId": "   "})
    assert result.decision is PolicyDecision.DENY


def test_calendar_event_update_is_allowed_when_if_match_present():
    result = evaluate(
        "calendar_event_update",
        {"eventId": "evt-1", "ifMatch": 'W/"abc123"'},
    )
    assert result.decision is PolicyDecision.ALLOW


def test_calendar_event_update_is_denied_when_if_match_missing():
    result = evaluate("calendar_event_update", {"eventId": "evt-1"})
    assert result.decision is PolicyDecision.DENY


def test_onedrive_item_update_is_allowed_when_if_match_present_for_existing_item():
    result = evaluate(
        "onedrive_item_update",
        {"itemId": "item-1", "ifMatch": '"etag-1"'},
    )
    assert result.decision is PolicyDecision.ALLOW


def test_onedrive_item_update_is_denied_when_if_match_missing():
    result = evaluate("onedrive_item_update", {"itemId": "item-1"})
    assert result.decision is PolicyDecision.DENY


def test_sharepoint_item_update_is_allowed_when_if_match_present_for_existing_item():
    result = evaluate(
        "sharepoint_item_update",
        {"itemId": "item-1", "ifMatch": '"etag-1"'},
    )
    assert result.decision is PolicyDecision.ALLOW


def test_sharepoint_item_update_is_denied_when_if_match_missing():
    result = evaluate("sharepoint_item_update", {"itemId": "item-1"})
    assert result.decision is PolicyDecision.DENY


@pytest.mark.parametrize("tool_name", UNCONDITIONALLY_REJECTED_TOOL_NAMES)
def test_unsafe_operations_are_denied_regardless_of_arguments(tool_name):
    # Even with arguments that "look" complete, these must fail closed.
    result = evaluate(tool_name, {"transactionId": "x", "ifMatch": "x", "id": "x"})
    assert result.decision is PolicyDecision.DENY


def test_unknown_tool_name_fails_closed():
    result = evaluate("some_never_registered_tool", {"anything": "goes"})
    assert result.decision is PolicyDecision.DENY


@pytest.mark.parametrize(
    "tool_name",
    [
        "mail-read___SearchMessages",
        "calendar-read___ListCalendarView",
        "teams-read___ListChatMessages",
        "sharepoint-read___findFileOrFolder",
        "onedrive-read___readSmallTextFileFromMyOnedrive",
    ],
)
def test_real_namespaced_workiq_read_tools_are_allowed(tool_name):
    assert evaluate(tool_name, {}).decision is PolicyDecision.ALLOW


def test_live_mail_create_draft_tool_is_allowed():
    result = evaluate(
        "workiq-mail___CreateDraftMessage",
        {"subject": "Draft only"},
    )
    assert result.decision is PolicyDecision.ALLOW


def test_live_mail_attachment_download_is_allowed_as_a_read():
    assert (
        evaluate("mail-read___DownloadAttachment", {"attachmentId": "a"}).decision
        is PolicyDecision.ALLOW
    )


def test_live_calendar_create_event_requires_transaction_id():
    allowed = evaluate(
        "workiq-calendar___CreateEvent",
        {"transactionId": "task-123"},
    )
    denied = evaluate(
        "workiq-calendar___CreateEvent",
        {"subject": "No id"},
    )

    assert allowed.decision is PolicyDecision.ALLOW
    assert denied.decision is PolicyDecision.DENY


@pytest.mark.parametrize(
    "tool_name",
    [
        "sharepoint-write___updateListItem",
        "mcp_SharePointRemoteServer_updateListItem",
    ],
)
def test_sharepoint_list_update_never_falls_through_to_the_list_read_heuristic(tool_name):
    assert evaluate(tool_name, {}).decision is PolicyDecision.DENY
    assert (
        evaluate(tool_name, {"etag": '"etag-1"'}).decision
        is PolicyDecision.ALLOW
    )


@pytest.mark.parametrize(
    "tool_name",
    [
        "sharepoint-write___renameFileOrFolder",
        "onedrive-write___renameFileOrFolderInMyOnedrive",
    ],
)
def test_allowlisted_rename_tools_require_an_etag(tool_name):
    assert evaluate(tool_name, {}).decision is PolicyDecision.DENY
    assert (
        evaluate(tool_name, {"etag": '"etag-1"'}).decision
        is PolicyDecision.ALLOW
    )


@pytest.mark.parametrize(
    "tool_name",
    [
        "workiq-mail___mcp_MailTools_graph_mail_sendMail",
        "workiq-mail___mcp_MailTools_graph_mail_reply",
        "workiq-teams___mcp_TeamsServer_graph_postMessage",
        "workiq-sharepoint___mcp_SharePointRemoteServer_graph_shareFileOrFolder",
        "workiq-onedrive___mcp_OneDriveRemoteServer_graph_createFolderInMyOnedrive",
        "workiq-calendar___mcp_CalendarTools_graph_cancelEvent",
    ],
)
def test_real_namespaced_unsafe_workiq_tools_are_denied(tool_name):
    assert evaluate(tool_name, {"transactionId": "x", "headers": {"If-Match": "x"}}).decision is PolicyDecision.DENY


@pytest.mark.parametrize(
    "tool_name",
    [
        "workiq-mail___GetMailboxRules",
        "workiq-calendar___ListCalendarPermissions",
        "workiq-onedrive___ReadSharingLinks",
    ],
)
def test_read_like_names_outside_the_catalog_allowlist_fail_closed(tool_name):
    assert evaluate(tool_name, {}).decision is PolicyDecision.DENY


def test_unknown_existing_item_update_does_not_inherit_etag_permission():
    result = evaluate(
        "workiq-onedrive___UpdateAnything",
        {"headers": {"If-Match": '"etag-1"'}},
    )

    assert result.decision is PolicyDecision.DENY
