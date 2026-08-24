"""Tests describing the desired root `azure.yaml` for the Chief of Staff agent.

These tests lock down the public source contract directly from `azure.yaml`:
the Chief of Staff hosted agent, Foundry catalog-backed Work IQ connections,
the `workiq-tools` toolbox, the disabled Routine, and ignore-file hygiene. No
azd or Foundry calls are made.

Schema references used to shape these assertions (all publicly verifiable):
  - `cli/azd/extensions/azure.ai.agents/schemas/azure.ai.agent.json` (Azure/azure-dev)
    for `activity.useCase` / `activity.publish.*` field names.
  - `cli/azd/extensions/azure.ai.routines/schemas/azure.ai.routine.json`
    (Azure/azure-dev) for routine `triggers.*` / `action.*` field names.
  - The Microsoft Foundry Work IQ catalog entries for target URLs,
    `UserEntraToken` audience, and `catalog_MCP` entity metadata.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

_SERVICE_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = _SERVICE_ROOT.parents[1]

AGENT_SERVICE_NAME = "chief-of-staff"
TOOLBOX_SERVICE_KEY = "workiq-tools"
ROUTINE_SERVICE_KEY = "weekday-chief-of-staff"
WORK_IQ_AUDIENCE = "ea9ffc3e-8a23-4a7d-836d-234d7c7565c1"
GITHUB_REPO_URL_FRAGMENT = "github.com/therealjohn/chief-of-staff"

# Installed azd extension beta floors (see `azd extension list --installed`).
REQUIRED_EXTENSION_FLOORS = {
    "azure.ai.agents": ">=1.0.0-beta.11",
    "azure.ai.projects": ">=1.0.0-beta.6",
    "azure.ai.connections": ">=1.0.0-beta.4",
    "azure.ai.toolboxes": ">=1.0.0-beta.5",
    "azure.ai.routines": ">=1.0.0-beta.4",
}

WORK_IQ_CONNECTIONS = {
    "workiq-mail": {
        "target": "https://agent365.svc.cloud.microsoft/agents/servers/mcp_MailTools",
        "entity": "microsoft-outlook-mail-mcp-frontier",
        "tools": {
            "DownloadAttachment",
            "GetAttachments",
            "GetMessage",
            "SearchMessages",
            "SearchMessagesQueryParameters",
            "CreateDraftMessage",
        },
    },
    "workiq-calendar": {
        "target": "https://agent365.svc.cloud.microsoft/agents/servers/mcp_CalendarTools",
        "entity": "microsoft-outlook-calendar-mcp-frontier",
        "tools": {
            "FindMeetingTimes",
            "GetOnlineMeetingAiInsights",
            "GetOnlineMeetingAttendanceReports",
            "GetOnlineMeetingTranscripts",
            "GetRooms",
            "GetUserDateAndTimeZoneSettings",
            "ListCalendarView",
            "ListEvents",
            "CreateEvent",
        },
    },
    "workiq-teams": {
        "target": "https://agent365.svc.cloud.microsoft/agents/servers/mcp_TeamsServer",
        "entity": "microsoft-teams-mcp-frontier",
        "tools": {
            "GetChatMessage",
            "ListChannelMessageReplies",
            "ListChannelMessages",
            "ListChannels",
            "ListChatMessages",
            "ListChats",
            "ListTeams",
            "SearchTeamMessagesQueryParameters",
            "SearchTeamsMessages",
        },
    },
    "workiq-sharepoint": {
        "target": "https://agent365.svc.cloud.microsoft/agents/servers/mcp_SharePointRemoteServer",
        "entity": "microsoft-sharepoint-mcp-frontier",
        "tools": {
            "findFileOrFolder",
            "findSite",
            "getDefaultDocumentLibraryInSite",
            "getFileOrFolderMetadata",
            "getFileOrFolderMetadataByUrl",
            "listDocumentLibrariesInSite",
            "readSmallBinaryFile",
            "readSmallTextFile",
            "renameFileOrFolder",
            "updateListItem",
        },
    },
    "workiq-onedrive": {
        "target": "https://agent365.svc.cloud.microsoft/agents/servers/mcp_OneDriveRemoteServer",
        "entity": "microsoft-onedrive-mcp-frontier",
        "tools": {
            "findFileOrFolderInMyDrive",
            "getFileOrFolderMetadataByUrl",
            "getFileOrFolderMetadataInMyOnedrive",
            "getOnedrive",
            "readSmallBinaryFileFromMyOnedrive",
            "readSmallTextFileFromMyOnedrive",
            "renameFileOrFolderInMyOnedrive",
        },
    },
}


def _normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _contains_all(normalized_name: str, substrings: set[str]) -> bool:
    return all(substring in normalized_name for substring in substrings)


# --------------------------------------------------------------------------
# azure.yaml parsing helpers
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def azure_yaml() -> dict[str, Any]:
    text = (_REPO_ROOT / "azure.yaml").read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    assert isinstance(doc, dict), "azure.yaml must parse to a mapping"
    return doc


def _services(doc: dict[str, Any]) -> dict[str, Any]:
    return doc.get("services") or {}


def _services_by_host(doc: dict[str, Any], host: str) -> dict[str, Any]:
    return {key: svc for key, svc in _services(doc).items() if isinstance(svc, dict) and svc.get("host") == host}


def _hosted_agent(doc: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    agents = _services_by_host(doc, "azure.ai.agent")
    assert len(agents) == 1, f"expected exactly one azure.ai.agent service, found {sorted(agents)}"
    return next(iter(agents.items()))


def _connections(doc: dict[str, Any]) -> dict[str, Any]:
    return _services_by_host(doc, "azure.ai.connection")


def _toolbox(doc: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    toolboxes = _services_by_host(doc, "azure.ai.toolbox")
    assert len(toolboxes) == 1, f"expected exactly one azure.ai.toolbox service, found {sorted(toolboxes)}"
    return next(iter(toolboxes.items()))


def _toolbox_entries_for_connection(toolbox: dict[str, Any], connection_key: str) -> list[dict[str, Any]]:
    return [tool for tool in toolbox.get("tools") or [] if tool.get("connection") == connection_key]


def _tool_names(entry: dict[str, Any]) -> list[str]:
    return (entry.get("allowed_tools") or {}).get("tool_names") or []


def _routine(doc: dict[str, Any]) -> dict[str, Any]:
    routine = _services(doc).get(ROUTINE_SERVICE_KEY)
    assert routine is not None, f"expected a {ROUTINE_SERVICE_KEY!r} azure.ai.routine service"
    return routine


def _first_trigger(routine: dict[str, Any]) -> dict[str, Any]:
    triggers = routine.get("triggers") or {}
    assert triggers, "routine declares no triggers"
    return next(iter(triggers.values()))


def _env_value(service: dict[str, Any], name: str) -> Any:
    for entry in service.get("environmentVariables") or []:
        if entry.get("name") == name:
            return entry.get("value")
    return None


# --------------------------------------------------------------------------
# ignore-file helpers (plain line scanning, not a full gitignore engine)
# --------------------------------------------------------------------------


def _ignore_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip() and not line.strip().startswith("#")]


def _excludes(lines: list[str], candidate: str) -> bool:
    for line in lines:
        rule = line.rstrip("/")
        if fnmatch.fnmatch(candidate, rule):
            return True
        if candidate == rule or candidate.startswith(rule + "/") or rule.endswith("/" + candidate):
            return True
    return False


# --------------------------------------------------------------------------
# Project / sole agent service identity
# --------------------------------------------------------------------------


def test_project_name_and_sole_agent_service_key_match(azure_yaml):
    assert azure_yaml.get("name") == AGENT_SERVICE_NAME
    hosted_key, agent = _hosted_agent(azure_yaml)
    assert hosted_key == AGENT_SERVICE_NAME
    assert agent.get("name") == AGENT_SERVICE_NAME


def test_agent_display_name_is_chief_of_staff(azure_yaml):
    _, agent = _hosted_agent(azure_yaml)
    assert agent.get("displayName") == "Chief of Staff"


def test_agent_description_fits_foundry_limit(azure_yaml):
    _, agent = _hosted_agent(azure_yaml)
    assert len(agent.get("description") or "") <= 512


# --------------------------------------------------------------------------
# requiredVersions
# --------------------------------------------------------------------------


@pytest.mark.parametrize("extension, floor", sorted(REQUIRED_EXTENSION_FLOORS.items()))
def test_required_version_floor_covers_installed_beta(azure_yaml, extension, floor):
    extensions = (azure_yaml.get("requiredVersions") or {}).get("extensions") or {}
    assert extensions.get(extension) == floor, f"requiredVersions.extensions[{extension!r}] must be {floor!r}"


# --------------------------------------------------------------------------
# ai-project
# --------------------------------------------------------------------------


def _ai_project(doc: dict[str, Any]) -> dict[str, Any]:
    project = _services(doc).get("ai-project")
    assert project is not None, "expected an 'ai-project' service"
    assert project.get("host") == "azure.ai.project"
    return project


def test_ai_project_does_not_hardcode_an_existing_project_endpoint(azure_yaml):
    project = _ai_project(azure_yaml)
    assert "endpoint" not in project, "public sample must not pin John's Foundry project endpoint"


def test_ai_project_deploys_public_default_gpt5(azure_yaml):
    project = _ai_project(azure_yaml)
    deployments = project.get("deployments") or []
    gpt5 = next((d for d in deployments if (d.get("model") or {}).get("name") == "gpt-5"), None)
    assert gpt5 is not None, f"expected a gpt-5 deployment, found {deployments}"
    assert gpt5["model"].get("format") == "OpenAI"
    assert gpt5["model"].get("version") == "2025-08-07"
    assert (gpt5.get("sku") or {}).get("name") == "GlobalStandard"
    assert (gpt5.get("sku") or {}).get("capacity") == 10


# --------------------------------------------------------------------------
# Hosted agent service
# --------------------------------------------------------------------------


def test_hosted_agent_is_direct_code_python_3_13_main(azure_yaml):
    _, agent = _hosted_agent(azure_yaml)
    assert agent.get("project") == "src/github-copilot-activity"
    assert agent.get("language") == "python"
    code_config = agent.get("codeConfiguration") or {}
    assert code_config.get("runtime") == "python_3_13"
    assert code_config.get("entryPoint") == "main.py"
    assert "docker" not in agent, "a direct-code agent must not also declare a docker: block"


def test_hosted_agent_uses_ai_project_all_connections_and_toolbox(azure_yaml):
    doc = azure_yaml
    _, agent = _hosted_agent(doc)
    expected = {"ai-project", TOOLBOX_SERVICE_KEY, *WORK_IQ_CONNECTIONS}
    assert set(agent.get("uses") or []) == expected


def test_hosted_agent_declares_activity_and_invocations_protocols(azure_yaml):
    _, agent = _hosted_agent(azure_yaml)
    protocols = {(p.get("protocol"), p.get("version")) for p in agent.get("protocols") or []}
    assert ("activity", "2.0.0") in protocols
    assert ("invocations", "1.0.0") in protocols


def test_hosted_agent_endpoint_is_activity_with_bot_service_rbac(azure_yaml):
    _, agent = _hosted_agent(azure_yaml)
    endpoint = agent.get("agentEndpoint") or {}
    assert "activity" in (endpoint.get("protocols") or [])
    scheme_types = {scheme.get("type") for scheme in endpoint.get("authorizationSchemes") or []}
    assert "BotServiceRbac" in scheme_types


def test_hosted_agent_model_env_defaults_to_gpt5(azure_yaml):
    _, agent = _hosted_agent(azure_yaml)
    assert _env_value(agent, "AZURE_AI_MODEL_DEPLOYMENT_NAME") == "${AZURE_AI_MODEL_DEPLOYMENT_NAME=gpt-5}"


def test_hosted_agent_toolbox_endpoint_env_references_workiq_tools(azure_yaml):
    _, agent = _hosted_agent(azure_yaml)
    assert _env_value(agent, "TOOLBOX_ENDPOINT") == "${TOOLBOX_WORKIQ_TOOLS_MCP_ENDPOINT}"


def test_hosted_agent_receives_azure_openai_endpoint_for_model_calls(azure_yaml):
    _, agent = _hosted_agent(azure_yaml)
    assert _env_value(agent, "AZURE_OPENAI_ENDPOINT") == "${AZURE_OPENAI_ENDPOINT}"


def test_hosted_agent_disables_code_tools_by_default(azure_yaml):
    _, agent = _hosted_agent(azure_yaml)
    assert _env_value(agent, "COPILOT_ENABLE_CODE_TOOLS") == (
        "${COPILOT_ENABLE_CODE_TOOLS=false}"
    )


# --------------------------------------------------------------------------
# activity.useCase / activity.publish (Teams package/publish metadata)
# --------------------------------------------------------------------------


def test_activity_use_case_is_simple(azure_yaml):
    _, agent = _hosted_agent(azure_yaml)
    activity = agent.get("activity") or {}
    assert activity.get("useCase") == "simple"


def test_activity_publish_metadata_identifies_chief_of_staff(azure_yaml):
    _, agent = _hosted_agent(azure_yaml)
    publish = (agent.get("activity") or {}).get("publish") or {}
    assert publish.get("agentDisplayName") == "Chief of Staff"
    assert publish.get("publishScope") == "shared"
    assert publish.get("appVersion") == "1.0.0"
    assert publish.get("developerName") == "John Miller"


@pytest.mark.parametrize("url_field", ["developerWebsiteUrl", "privacyUrl", "termsOfUseUrl"])
def test_activity_publish_urls_point_at_the_github_repo(azure_yaml, url_field):
    _, agent = _hosted_agent(azure_yaml)
    publish = (agent.get("activity") or {}).get("publish") or {}
    url = publish.get(url_field)
    assert url, f"activity.publish.{url_field} is not set"
    assert GITHUB_REPO_URL_FRAGMENT in url


# --------------------------------------------------------------------------
# Foundry catalog-backed Work IQ connections
# --------------------------------------------------------------------------


def test_exactly_five_workiq_catalog_connections(azure_yaml):
    connections = _connections(azure_yaml)
    assert set(connections) == set(WORK_IQ_CONNECTIONS)


@pytest.mark.parametrize("connection_key", sorted(WORK_IQ_CONNECTIONS))
def test_workiq_catalog_connection_shape(azure_yaml, connection_key):
    expected = WORK_IQ_CONNECTIONS[connection_key]
    conn = _services(azure_yaml)[connection_key]
    assert conn.get("uses") == ["ai-project"]
    assert conn.get("category") == "RemoteTool"
    assert conn.get("authType") == "UserEntraToken"
    assert conn.get("audience") == WORK_IQ_AUDIENCE
    assert conn.get("target") == expected["target"]
    metadata = conn.get("metadata") or {}
    assert metadata.get("type") == "catalog_MCP"
    assert metadata.get("toolEntityId") == (
        "azureml://location/eastus/apiCenter/registry-prod-bl/type/tools/"
        f"objectId/{expected['entity']}/version/1"
    )


# --------------------------------------------------------------------------
# workiq-tools toolbox
# --------------------------------------------------------------------------


def test_toolbox_service_key_is_workiq_tools(azure_yaml):
    toolbox_key, _ = _toolbox(azure_yaml)
    assert toolbox_key == TOOLBOX_SERVICE_KEY


def test_toolbox_uses_catalog_workiq_connections_and_safe_tools(azure_yaml):
    _, toolbox = _toolbox(azure_yaml)
    assert set(toolbox.get("uses") or []) == {"ai-project", *WORK_IQ_CONNECTIONS}
    entries = toolbox.get("tools") or []
    for connection_key, expected in WORK_IQ_CONNECTIONS.items():
        connection_entries = _toolbox_entries_for_connection(
            toolbox,
            connection_key,
        )
        assert connection_entries
        assert all(entry.get("type") == "mcp" for entry in connection_entries)
        assert all(entry.get("require_approval") == "never" for entry in connection_entries)
        assert all(entry.get("server_url") == expected["target"] for entry in connection_entries)
        allowed = {
            name
            for entry in connection_entries
            for name in _tool_names(entry)
        }
        assert allowed == expected["tools"]


def test_toolbox_excludes_unsafe_catalog_operations(azure_yaml):
    _, toolbox = _toolbox(azure_yaml)
    names = {
        name.lower()
        for entry in toolbox.get("tools") or []
        for name in _tool_names(entry)
    }
    assert not any(
        fragment in name
        for name in names
        for fragment in ("send", "reply", "forward", "delete", "post", "share")
    )


def test_toolbox_server_labels_are_unique(azure_yaml):
    _, toolbox = _toolbox(azure_yaml)
    labels = [entry.get("server_label") for entry in toolbox.get("tools") or []]
    assert labels, "toolbox declares no tools"
    assert len(labels) == len(set(labels)), f"duplicate server_label values: {labels}"


# --------------------------------------------------------------------------
# weekday-chief-of-staff routine
# --------------------------------------------------------------------------


def test_routine_is_disabled_and_uses_the_agent(azure_yaml):
    doc = azure_yaml
    hosted_key, _ = _hosted_agent(doc)
    routine = _routine(doc)
    assert routine.get("host") == "azure.ai.routine"
    assert routine.get("enabled") is False
    assert routine.get("uses") == [hosted_key]


def test_routine_schedule_is_weekday_8am_new_york(azure_yaml):
    routine = _routine(azure_yaml)
    trigger = _first_trigger(routine)
    assert trigger.get("type") == "schedule"
    assert trigger.get("cron_expression") == "0 8 * * 1-5"
    assert trigger.get("time_zone") == "America/New_York"


def test_routine_action_invokes_invocations_api_for_the_agent(azure_yaml):
    doc = azure_yaml
    hosted_key, agent = _hosted_agent(doc)
    routine = _routine(doc)
    action = routine.get("action") or {}
    assert action.get("type") == "invoke_agent_invocations_api"
    assert action.get("agent_name") == agent.get("name") == hosted_key


def test_routine_notification_is_generic_and_does_not_claim_prior_work_iq_access(azure_yaml):
    routine = _routine(azure_yaml)
    payload = (routine.get("action") or {}).get("input")
    assert isinstance(payload, dict), "routine action.input must be an object"
    assert set(payload) == {"notification"}, "the only key must be the generic 'notification' field"

    notification = payload["notification"]
    assert isinstance(notification, str) and notification.strip(), "notification must be a nonblank string"

    lowered = notification.lower()
    assert "brief" in lowered, "notification should ask the user to request/prepare a briefing"
    assert "already" not in lowered, "notification must not claim the briefing work was already done"
    assert "work iq" not in lowered, "notification must stay generic and not name Work IQ"


# --------------------------------------------------------------------------
# infra provider
# --------------------------------------------------------------------------


def test_infra_provider_is_microsoft_foundry(azure_yaml):
    assert (azure_yaml.get("infra") or {}).get("provider") == "microsoft.foundry"


# --------------------------------------------------------------------------
# Service .agentignore hygiene
# --------------------------------------------------------------------------

_AGENTIGNORE_PATH = _SERVICE_ROOT / ".agentignore"


@pytest.fixture(scope="module")
def agentignore_lines() -> list[str]:
    return _ignore_lines(_AGENTIGNORE_PATH)


def test_service_agentignore_file_exists():
    assert _AGENTIGNORE_PATH.exists(), ".agentignore is missing from src/github-copilot-activity"


@pytest.mark.parametrize(
    "pattern",
    [
        ".env",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        "tests",
        "requirements-dev.txt",
        "Dockerfile",
        ".dockerignore",
        "appPackage.zip",
        ".appPackage.zip.azd-generated",
        "TEAMS_APP_SETUP.md",
    ],
)
def test_service_agentignore_excludes_dev_only_paths(agentignore_lines, pattern):
    assert _excludes(agentignore_lines, pattern), f".agentignore does not exclude {pattern!r}"


def test_service_agentignore_does_not_exclude_production_code_or_requirements(agentignore_lines):
    assert not _excludes(agentignore_lines, "main.py"), ".agentignore must not exclude production main.py"
    assert not _excludes(agentignore_lines, "client.py"), ".agentignore must not exclude production client.py"
    assert not _excludes(agentignore_lines, "requirements.txt"), ".agentignore must not exclude requirements.txt"


# --------------------------------------------------------------------------
# Root .gitignore hygiene
# --------------------------------------------------------------------------

_GITIGNORE_PATH = _REPO_ROOT / ".gitignore"


@pytest.fixture(scope="module")
def gitignore_lines() -> list[str]:
    return _ignore_lines(_GITIGNORE_PATH)


def test_root_gitignore_ignores_app_package_zip(gitignore_lines):
    assert _excludes(gitignore_lines, "appPackage.zip")


def test_root_gitignore_ignores_azd_package_marker(gitignore_lines):
    assert _excludes(gitignore_lines, ".appPackage.zip.azd-generated")


def test_root_gitignore_ignores_generated_teams_app_setup_doc(gitignore_lines):
    assert _excludes(gitignore_lines, "TEAMS_APP_SETUP.md")
