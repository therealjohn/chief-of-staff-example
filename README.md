# Chief of Staff for Microsoft Teams

A public-ready Python example of a Microsoft Foundry hosted agent that runs as
a personal Microsoft Teams app. The agent acts as a Chief of Staff, uses the
GitHub Copilot SDK for its model and tool loop, and accesses Microsoft 365
through a Foundry Toolbox backed by catalog-managed Work IQ tools.

> [!WARNING]
> This is preview example code, not a production reference. Resilient task
> recovery can repeat model work or a proactive notification. The safe default
> exposes only skills and read-only file tools; enabling code tools opts into
> auto-approved shell, Python, and file mutation. Review the security and
> reliability notes before adapting this code.

## Quickstart

This is the shortest supported path for a first deployment. It creates a new
Foundry project, deploys the Chief of Staff, and publishes it to Teams.
Use the [advanced setup](#advanced-setup) only when you need an existing
Foundry project or local development. You do not need Docker, a local Python
environment, or a local `.env` for this path.

### 1. Install the prerequisites

Install [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) and
[Azure Developer CLI](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd)
1.31.2 or later. You also need:

- An Azure subscription with access to Foundry hosted agents and permission to
  deploy the declared resources.
- A Microsoft 365 tenant in the same Entra directory with Microsoft 365
  Copilot.

### 2. Deploy a new Foundry project

From a PowerShell prompt in the repository root, install the required azd
extension, sign in, and deploy:

```powershell
azd ext install microsoft.foundry

az login
azd auth login
azd up
```

Follow the `azd up` prompts to choose an environment name, subscription, and a
region with `gpt-5` capacity. The command provisions the Foundry project,
model, Work IQ connections, toolbox, hosted agent, Azure Bot, and disabled
weekday Routine. The azd AI agent post-deploy hook grants the hosted agent
access to models in the same project, so no separate role assignment is
required.

### 3. Publish to Teams

Publish the app for shared distribution after `azd up` completes:

```powershell
azd ai agent publish chief-of-staff --scope shared --display-name "Chief of Staff" --app-version "1.0.0"
```

The Microsoft 365 service builds and publishes the Teams package server-side,
so there is no ZIP to upload manually. Use the published app details returned
by the command to open and add **Chief of Staff** in Teams, then start a
personal chat. Send a normal message and request a briefing in natural
language. Work IQ may prompt for consent the first time it accesses Microsoft
365.

## What the sample demonstrates

The sample demonstrates:

- Personal Teams conversations over the Activity protocol.
- Streaming text and informative updates that follow the Teams streaming UX.
- Deferred completion: if no text arrives within eight seconds, the bot ends
  the turn with `That'll take me a bit. I'll notify you when I'm done.` and
  sends the completed answer as a proactive message.
- Preview Foundry resilient tasks for work that outlives the original Activity
  request.
- Durable registration of every personal Teams installation in
  `FoundryStateStore`.
- A second Invocations protocol used by a disabled, source-controlled Foundry
  Routine to broadcast a generic notification.
- Work IQ access to Outlook Mail, Outlook Calendar, Teams, SharePoint, and
  OneDrive through resources declared in `azure.yaml`.
- An on-demand `chief-of-staff-briefing` Skill that expands relevant messages
  and threads, correlates activity across sources, ranks priorities, and
  produces evidence-linked briefings without padding empty sections.
- The upstream sample's to-do Adaptive Card, shared-file input, image
  understanding, generated-file delivery, and Copilot shell/Python/file tools.

## How it works

```text
Teams
  -> Azure Bot created by azd
  -> Foundry Activity endpoint
  -> Python Activity handler
  -> GitHub Copilot SDK
  -> gpt-5
  -> Foundry Toolbox
  -> Work IQ (Mail, Calendar, Teams, SharePoint, OneDrive)
```

The hosted process exposes two protocols:

| Protocol | Purpose |
| --- | --- |
| Activity 2.0 | Teams chat, streaming replies, installation events, Adaptive Cards, file consent, and proactive delivery |
| Invocations 1.0 | Generic notification broadcasts from Foundry Routines or another trusted trigger |

Each personal install is recorded as an **installation recipient**. A normal
message starts a resilient task and an event broker:

1. Informative updates can appear while the agent is working.
2. If the first text chunk arrives within eight seconds, the answer continues
   as a normal Teams stream.
3. If no text arrives within eight seconds, the stream ends with the deferred
   acknowledgement.
4. The task continues and sends the complete result through the stored Teams
   conversation reference.

Only one task runs per personal conversation. Send `cancel` to stop the active
task. Teams' Stop control is also honored when the SDK reports stream
cancellation.

## Work IQ and the scheduled Routine

Work IQ uses delegated `UserEntraToken` connections. It works only when a
signed-in user is present on a live Teams Activity. A scheduled Foundry Routine
has no signed-in user, so it cannot read that person's mail, calendar, Teams,
SharePoint, or OneDrive data.

The checked-in weekday Routine therefore broadcasts a generic prompt:

> Good morning! Reply here if you'd like me to prepare your daily briefing.

The user's reply creates a live turn with delegated identity. The Chief of Staff
then builds a grounded briefing from today's calendar and the previous 24 hours
of mail and Teams activity, covering priorities, meeting preparation, important
decisions, risks, and next actions. The runtime loads
`src/github-copilot-activity/skills/chief-of-staff-briefing/SKILL.md` through
the Copilot SDK. That Skill is the single source of truth for briefing
retrieval, synthesis, grounding, and presentation.

The Routine is deployed disabled. It stays disabled until you explicitly enable
it.

## Work IQ safety boundary

The toolbox uses Foundry catalog-backed Work IQ Mail, Calendar, Teams, SharePoint, and OneDrive connections. It explicitly allowlists the read operations needed for briefings and exposes only these narrow write operations behind the runtime policy:

- Create an Outlook draft.
- Create a calendar event with a `transactionId`.
- Rename an existing SharePoint or OneDrive item with an ETag.
- Update an existing SharePoint list item with an ETag.

The toolbox does not expose mail send/reply, Teams posting, sharing,
cancellation, destructive deletes, folder creation, or copy operations. The
runtime applies a second fail-closed policy before every Work IQ call.

This is not a complete authorization system. Work IQ tool names and schemas are
preview surfaces and can change.

## Customize the example

The main customization seams are intentionally small:

| Goal | Change |
| --- | --- |
| Change the persona or briefing format | `src/github-copilot-activity/skills/chief-of-staff-briefing/SKILL.md` |
| Change model deployment | `services.ai-project.deployments` and `AZURE_AI_MODEL_DEPLOYMENT_NAME` in `azure.yaml` |
| Add or remove Microsoft 365 capabilities | Work IQ connections/tool allowlists in `azure.yaml`, then update `workiq_policy.py` |
| Change long-running behavior | `long_running.py`; keep the Foundry adapter in `foundry_work.py` thin |
| Change proactive recipient storage | Implement the `RecipientRepository` interface in `recipient_repository.py` |
| Enable generated-file workflows | Set `COPILOT_ENABLE_CODE_TOOLS=true` only after reviewing the security implications |
| Change Teams metadata | `activity.publish` in `azure.yaml`, then increment `appVersion` |
| Enable scheduled notifications | Review the Routine limitations, then explicitly enable `weekday-chief-of-staff` |

Retrieved mail, meetings, chats, and files are untrusted data. The system prompt
and briefing Skill instruct the model never to follow embedded instructions,
and `workiq_policy.py` independently gates every Work IQ write.

## Advanced setup

The Quickstart is the recommended first-run path. Use this section to deploy
into an existing Foundry project, configure the environment explicitly, or run
the agent locally.

### Prerequisites

- An Azure subscription with access to Microsoft Foundry hosted agents.
- A Microsoft 365 tenant in the same Entra directory.
- A Microsoft 365 Copilot license for each Work IQ user.
- Python 3.13 for local development.
- [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli).
- [Azure Developer CLI](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd)
  1.31.2 or later.

Install the required azd extension:

```powershell
azd ext install microsoft.foundry
```

Sign in yourself:

```powershell
az login
azd auth login
```

### Configure an azd environment

The repository does not contain an Azure subscription, project endpoint,
tenant, or other deployment-specific state. That state lives under the ignored
`.azure/` directory.

#### Use a new Foundry project

```powershell
azd env new dev
azd env set AZURE_SUBSCRIPTION_ID "<subscription-id>"
azd env set AZURE_LOCATION "<supported-region>"
```

`azure.yaml` declares the `gpt-5` deployment, five Foundry catalog-backed Work IQ connections, toolbox, hosted agent, and disabled Routine. `azd up` provisions and deploys them.

#### Use an existing Foundry project

Create the environment, then bind it to the existing project:

```powershell
azd env new dev
azd env set AZURE_SUBSCRIPTION_ID "<subscription-id>"
azd env set AZURE_LOCATION "<project-region>"
azd env set AZURE_AI_PROJECT_ID "<full-project-arm-resource-id>"
azd env set AZURE_AI_PROJECT_ENDPOINT "https://<account>.services.ai.azure.com/api/projects/<project>"
azd env set FOUNDRY_PROJECT_ENDPOINT "https://<account>.services.ai.azure.com/api/projects/<project>"
azd env set AZURE_AI_MODEL_DEPLOYMENT_NAME "gpt-5"
azd env set USE_EXISTING_AI_PROJECT "true"
```

The existing project must already have a deployment named `gpt-5`, or you must
change both `azure.yaml` and `AZURE_AI_MODEL_DEPLOYMENT_NAME`.

The five Work IQ `azure.ai.connection` services are infrastructure resources. Avoid `azd provision` when binding this manifest to an existing project because its new-project deployment block can create another Foundry account. Materialize only the declared catalog connections:

```powershell
$project = "https://<account>.services.ai.azure.com/api/projects/<project>"
$audience = "ea9ffc3e-8a23-4a7d-836d-234d7c7565c1"
$catalog = "azureml://location/eastus/apiCenter/registry-prod-bl/type/tools/objectId"

azd ai connection create workiq-mail --kind remote-tool --target "https://agent365.svc.cloud.microsoft/agents/servers/mcp_MailTools" --auth-type user-entra-token --audience $audience --metadata type=catalog_MCP --metadata "toolEntityId=$catalog/microsoft-outlook-mail-mcp-frontier/version/1" --project-endpoint $project --no-prompt
azd ai connection create workiq-calendar --kind remote-tool --target "https://agent365.svc.cloud.microsoft/agents/servers/mcp_CalendarTools" --auth-type user-entra-token --audience $audience --metadata type=catalog_MCP --metadata "toolEntityId=$catalog/microsoft-outlook-calendar-mcp-frontier/version/1" --project-endpoint $project --no-prompt
azd ai connection create workiq-teams --kind remote-tool --target "https://agent365.svc.cloud.microsoft/agents/servers/mcp_TeamsServer" --auth-type user-entra-token --audience $audience --metadata type=catalog_MCP --metadata "toolEntityId=$catalog/microsoft-teams-mcp-frontier/version/1" --project-endpoint $project --no-prompt
azd ai connection create workiq-sharepoint --kind remote-tool --target "https://agent365.svc.cloud.microsoft/agents/servers/mcp_SharePointRemoteServer" --auth-type user-entra-token --audience $audience --metadata type=catalog_MCP --metadata "toolEntityId=$catalog/microsoft-sharepoint-mcp-frontier/version/1" --project-endpoint $project --no-prompt
azd ai connection create workiq-onedrive --kind remote-tool --target "https://agent365.svc.cloud.microsoft/agents/servers/mcp_OneDriveRemoteServer" --auth-type user-entra-token --audience $audience --metadata type=catalog_MCP --metadata "toolEntityId=$catalog/microsoft-onedrive-mcp-frontier/version/1" --project-endpoint $project --no-prompt

azd env set AZURE_AI_PROJECT_CONNECTION_NAMES "workiq-mail,workiq-calendar,workiq-teams,workiq-sharepoint,workiq-onedrive"
```

The `metadata.type: catalog_MCP` and `toolEntityId` values are required. Omitting them turns the same URLs into generic custom MCP connections, which use a different entitlement path and are not the Foundry catalog tools shown in the portal.

## Run locally

Copy the example environment file and set the project endpoint and model:

```powershell
Copy-Item src\github-copilot-activity\.env.example src\github-copilot-activity\.env
```

Create the virtual environment next to `requirements.txt`, as required by
`azd ai agent run`:

```powershell
Set-Location src\github-copilot-activity
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install uv
Set-Location ..\..
```

Start the Activity agent and Microsoft 365 Agents Playground:

```powershell
azd ai agent run chief-of-staff
```

For a headless startup check:

```powershell
azd ai agent run chief-of-staff --no-client
```

The readiness endpoint is `http://localhost:8088/readiness`.

The Invocations protocol can be checked locally while the server is running:

```powershell
@'
{"notification":"Test notification"}
'@ | Set-Content notification.json

azd ai agent invoke chief-of-staff --local --protocol invocations --input-file notification.json
```

Local Activity testing uses Microsoft 365 Agents Playground. A real proactive
send and delegated Work IQ call require the deployed Teams channel.

## Test

```powershell
Set-Location src\github-copilot-activity
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest tests -q
```

## Deploy

For a new Foundry project, from the repository root:

```powershell
azd up
```

For an existing Foundry project, complete the connection bootstrap above, then
deploy without provisioning:

```powershell
azd deploy --no-prompt
```

The agent uses direct code deployment, so Docker is not required.

For an existing Foundry project, grant the project's managed identity
account-scoped `Foundry User` before deployment. This is the standard
project-to-account assignment used for model access:

```powershell
$projectId = azd env get-value AZURE_AI_PROJECT_ID
$accountId = $projectId -replace "/projects/[^/]+$", ""
$projectIdentity = az rest `
  --method get `
  --url "https://management.azure.com$projectId?api-version=2025-06-01" `
  --query identity.principalId `
  --output tsv

az role assignment create `
  --assignee-object-id $projectIdentity `
  --assignee-principal-type ServicePrincipal `
  --role "Foundry User" `
  --scope $accountId

azd deploy --no-prompt
```

The azd AI agent post-deploy hook grants the deployed hosted-agent identity access to models in the selected project. No separate `Cognitive Services OpenAI User` assignment is required for the hosted agent.

Toolbox versions are immutable. The first deployment publishes version 1 by
default. After changing `workiq-tools` in `azure.yaml`, deploy and promote the
new version, then redeploy the agent so its `TOOLBOX_ENDPOINT` points at that
version:

```powershell
azd deploy workiq-tools --no-prompt
azd ai toolbox versions list workiq-tools --output json
azd ai toolbox publish workiq-tools "<new-version>"
azd deploy chief-of-staff --no-prompt
```

Confirm the deployment:

```powershell
azd ai agent show --output json
azd ai agent doctor --output json
azd ai toolbox show workiq-tools --output json
azd ai routine show weekday-chief-of-staff --output json
```

## Publish to Teams

For normal distribution, publish the app directly through the Microsoft 365
service:

```powershell
azd ai agent publish chief-of-staff --scope shared --display-name "Chief of Staff" --app-version "1.0.0"
```

The `shared` scope makes the app available for shareable-link distribution
without tenant-admin approval. Use `--scope tenant` instead to publish it to
the organization-wide catalog; that scope requires IT administrator approval.
Neither path requires a local ZIP upload.

### Inspect a local package

Use `pack` only when you explicitly need a local package for inspection or
personal-sideload diagnostics. It writes:

- `src/github-copilot-activity/appPackage.zip`
- `src/github-copilot-activity/TEAMS_APP_SETUP.md`

This repository ignores both generated files because they contain
deployment-specific identifiers. With `azure.ai.agents` 1.0.0-beta.11, the
best-effort package can use the agent resource name instead of
`activity.publish.agentDisplayName` and can include a non-interactive Copilot
recipient beside the Teams bot. Always regenerate the package explicitly after
deployment, increment `--app-version` for an installed app, normalize it to the
personal Teams bot, and then follow the generated `TEAMS_APP_SETUP.md`:

```powershell
azd ai agent pack chief-of-staff --scope personal --display-name "Chief of Staff" --app-version "1.0.0"
src\github-copilot-activity\.venv\Scripts\python.exe src\github-copilot-activity\scripts\finalize_teams_package.py src\github-copilot-activity\appPackage.zip
```

The normalization step removes `copilotAgents` and restricts the manifest bot scopes to `personal`, so Teams exposes one working Chief of Staff recipient instead of a second recipient that cannot be used in chat.

## Test proactive behavior

After opening the published app in a personal chat:

1. Send a short message and confirm a normal streamed response.
2. Ask for a briefing in natural language and confirm the answer uses Work IQ sources.
3. Request work that takes more than eight seconds without producing text.
   Confirm the deterministic acknowledgement appears, followed later by a new
   proactive message.
4. Start another long request and send `cancel`.
5. Dispatch the disabled Routine manually:

```powershell
azd ai routine dispatch weekday-chief-of-staff --input '{"notification":"Good morning! Reply here if you would like me to prepare your daily briefing."}'
azd ai routine run list weekday-chief-of-staff
```

The manual dispatch uses the supplied payload only for that run. The checked-in
Routine remains disabled. To enable the weekday schedule explicitly:

```powershell
azd ai routine enable weekday-chief-of-staff
```

## Important limitations

- Foundry hosted agents, resilient tasks, Work IQ MCP servers, Toolboxes,
  Routines, and Teams publishing are preview features.
- Work IQ is available only on live user turns. Routine notifications do not
  contain unattended Microsoft 365 data.
- Resilient task handlers can restart from the beginning. This example does not
  implement retries, effect deduplication, or exactly-once delivery.
- Teams streaming has a two-minute limit. This example does not add a second
  transition for streams that exceed that limit.
- All personal installations receive Routine broadcasts.
- Conversation recipient records do not expire. Uninstall removes the
  corresponding record, and confirmed blocked recipients are pruned.
- Shell, Python, and file-mutation tools are disabled by default. Setting
  `COPILOT_ENABLE_CODE_TOOLS=true` exposes and auto-approves the Copilot SDK
  built-ins; use that mode only in a reviewed, isolated environment.
- Task and shared-file storage rely on Foundry's per-session persistent
  filesystem isolation. The app hashes conversation identifiers before using
  them as local paths, but it is not a general multi-tenant storage layer.
- Work IQ-dependent requests fail closed when delegated identity or toolbox
  access is unavailable. General model conversation can still work.

## Project layout

```text
.
|-- azure.yaml
|-- CONTEXT.md
|-- LICENSE
|-- AGENTS.md
|-- CONTRIBUTING.md
|-- SECURITY.md
`-- src/github-copilot-activity/
    |-- activity_values.py
    |-- agent_runtime.py
    |-- cards.py
    |-- main.py
    |-- client.py
    |-- copilot_sessions.py
    |-- files.py
    |-- foundry_work.py
    |-- invocations.py
    |-- long_running.py
    |-- mcp_bridge.py
    |-- notification_delivery.py
    |-- notifications.py
    |-- outfiles.py
    |-- proactive_delivery.py
    |-- recipient_repository.py
    |-- task_coordinator.py
    |-- toolbox_runtime.py
    |-- toolbox_tools.py
    |-- tools.py
    |-- workiq_policy.py
    |-- scripts/finalize_teams_package.py
    |-- skills/chief-of-staff-briefing/SKILL.md
    `-- tests/
```

## Upstream

This repository is adapted from Microsoft's
[GitHub Copilot Activity hosted-agent sample](https://github.com/microsoft-foundry/foundry-samples/tree/main/samples/python/hosted-agents/bring-your-own/activity/github-copilot).
Adapted Microsoft files retain their original copyright notices.

## License

MIT. See [LICENSE](LICENSE).
