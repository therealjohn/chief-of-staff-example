"""Work IQ hard policy: a fail-closed allow/deny gate for tool calls.

A structured classifier maps concrete tool names (+ arguments) to an
allow/deny decision, independent of any real connector. Unknown/unrecognized
tool names fail CLOSED (denied).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any


class PolicyDecision(Enum):
    ALLOW = auto()
    DENY = auto()


@dataclass
class PolicyResult:
    decision: PolicyDecision
    reason: str


_READ_TOOL_NAMES = {
    "mail_search",
    "mail_read",
    "calendar_read",
    "calendar_list",
    "onedrive_read",
    "sharepoint_read",
    "people_search",
    "teams_read_messages",
}

# Explicitly, unconditionally rejected regardless of arguments.
_UNCONDITIONALLY_REJECTED_TOOL_NAMES = {
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
}

_CONDITIONAL_IF_MATCH_TOOL_NAMES = {
    "calendar_event_update",
    "onedrive_item_update",
    "sharepoint_item_update",
}

_UNSAFE_NAME_FRAGMENTS = (
    "sendmail",
    "senddraft",
    "replyall",
    "reply",
    "forward",
    "postmessage",
    "postchannelmessage",
    "sharefile",
    "sharefolder",
    "cancelevent",
    "acceptevent",
    "declineevent",
    "delete",
    "createfolder",
    "copyfile",
    "copyfolder",
    "movefile",
    "movefolder",
    "uploadfile",
    "createlist",
    "createchat",
    "createchannel",
    "addchatmember",
    "addchannelmember",
    "sendinvite",
)

_CATALOG_READ_TOOL_NAMES = {
    "downloadattachment",
    "getattachments",
    "getmessage",
    "searchmessages",
    "searchmessagesqueryparameters",
    "findmeetingtimes",
    "getonlinemeetingaiinsights",
    "getonlinemeetingattendancereports",
    "getonlinemeetingtranscripts",
    "getrooms",
    "getuserdateandtimezonesettings",
    "listcalendarview",
    "listevents",
    "getchatmessage",
    "listchannelmessagereplies",
    "listchannelmessages",
    "listchannels",
    "listchatmessages",
    "listchats",
    "listteams",
    "searchteammessagesqueryparameters",
    "searchteamsmessages",
    "findfileorfolder",
    "findsite",
    "getdefaultdocumentlibraryinsite",
    "getfileorfoldermetadata",
    "getfileorfoldermetadatabyurl",
    "listdocumentlibrariesinsite",
    "readsmallbinaryfile",
    "readsmalltextfile",
    "findfileorfolderinmydrive",
    "getfileorfoldermetadatainmyonedrive",
    "getonedrive",
    "readsmallbinaryfilefrommyonedrive",
    "readsmalltextfilefrommyonedrive",
}


def _non_blank_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _normalized_tool_name(tool_name: str) -> str:
    base = tool_name.rsplit("___", 1)[-1].rsplit(".", 1)[-1]
    return re.sub(r"[^a-z0-9]", "", base.lower())


def _has_nested_value(arguments: Any, names: set[str]) -> bool:
    if isinstance(arguments, dict):
        for key, value in arguments.items():
            normalized_key = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized_key in names and _non_blank_string(value):
                return True
            if _has_nested_value(value, names):
                return True
    elif isinstance(arguments, list):
        return any(_has_nested_value(value, names) for value in arguments)
    return False


def evaluate(tool_name: str, arguments: dict[str, Any]) -> PolicyResult:
    """Evaluate a tool call against the Work IQ hard policy."""
    if tool_name in _READ_TOOL_NAMES:
        return PolicyResult(PolicyDecision.ALLOW, "read operations are always allowed")

    if tool_name == "outlook_draft_create":
        return PolicyResult(PolicyDecision.ALLOW, "Outlook draft creation is always allowed")

    if tool_name == "calendar_event_create":
        if _non_blank_string(arguments.get("transactionId")):
            return PolicyResult(PolicyDecision.ALLOW, "transactionId present")
        return PolicyResult(PolicyDecision.DENY, "transactionId missing or blank")

    if tool_name in _CONDITIONAL_IF_MATCH_TOOL_NAMES:
        if _non_blank_string(arguments.get("ifMatch")):
            return PolicyResult(PolicyDecision.ALLOW, "conditional update (If-Match/eTag present)")
        return PolicyResult(PolicyDecision.DENY, "If-Match/eTag missing or blank")

    if tool_name in _UNCONDITIONALLY_REJECTED_TOOL_NAMES:
        return PolicyResult(PolicyDecision.DENY, "unsafe operation is always denied")

    normalized_name = _normalized_tool_name(tool_name)
    if any(fragment in normalized_name for fragment in _UNSAFE_NAME_FRAGMENTS):
        return PolicyResult(PolicyDecision.DENY, "unsafe operation is always denied")

    if normalized_name in _CATALOG_READ_TOOL_NAMES:
        return PolicyResult(PolicyDecision.ALLOW, "read operation")

    if normalized_name.endswith("createdraftmessage"):
        return PolicyResult(PolicyDecision.ALLOW, "Outlook draft creation is allowed")

    if normalized_name.endswith("createevent"):
        if _has_nested_value(arguments, {"transactionid"}):
            return PolicyResult(PolicyDecision.ALLOW, "transactionId present")
        return PolicyResult(PolicyDecision.DENY, "transactionId missing or blank")

    if normalized_name.endswith(
        ("updatelistitem", "renamefileorfolder", "renamefileorfolderinmyonedrive")
    ):
        if _has_nested_value(arguments, {"ifmatch", "etag"}):
            return PolicyResult(PolicyDecision.ALLOW, "conditional existing-item update")
        return PolicyResult(PolicyDecision.DENY, "If-Match/eTag missing or blank")

    return PolicyResult(PolicyDecision.DENY, "unknown tool name fails closed")
