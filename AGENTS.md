# Agent Instructions

This project was built with the microsoft-foundry skill. Before working on or
answering questions about Foundry agents, read the microsoft-foundry skill
first.

## Context

Read [CONTEXT.md](CONTEXT.md) when changing product language, recipient
behavior, deferred delivery, or Routine behavior.

## Structure

- `azure.yaml` is the source of truth for the Foundry project, model,
  connections, toolbox, hosted agent, Teams metadata, and disabled Routine.
- `src/github-copilot-activity/main.py` owns protocol handlers and composition.
- `copilot_sessions.py` owns Copilot client, session, prompt, and toolbox
  lifecycle.
- `long_running.py` owns streaming-versus-deferred behavior.
- `foundry_work.py` is the thin resilient-task adapter.
- `workiq_policy.py` and `azure.yaml` jointly enforce the Work IQ tool boundary.

## Invariants

- Teams scope is personal chat only.
- Work IQ runs only on live user Activities; Routines broadcast generic
  notifications and never claim unattended Work IQ access.
- Keep `x-agent-foundry-call-id` opaque and forward it only through the current
  request context.
- Unknown or unsafe Work IQ tools fail closed.
- Keep the Activity and Invocations protocols on the same hosted service.
- Keep the weekday Routine disabled in source.
- Preserve the full upstream card and file-consent flows.

## Development

Use Python 3.13 and the service-local `.venv`.

```powershell
Set-Location src\github-copilot-activity
.\.venv\Scripts\python.exe -m pytest tests -q
```

Use test-first development for behavior changes. Keep public configuration free
of subscription IDs, tenant IDs, project endpoints, secrets, and generated
`appPackage.zip` files.
