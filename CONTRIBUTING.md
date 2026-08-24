# Contributing

This repository is a focused example of a personal Teams Chief of Staff on Microsoft Foundry. Keep changes small, testable, and aligned with the invariants in [AGENTS.md](AGENTS.md) and the product language in [CONTEXT.md](CONTEXT.md).

## Local setup

Use Python 3.13 and the service-local virtual environment:

```powershell
Set-Location src\github-copilot-activity
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest tests -q
```

## Change guidelines

- Add a failing test before changing behavior.
- Keep protocol composition in `main.py`, Copilot lifecycle in `copilot_sessions.py`, long-running behavior in `long_running.py`, and the Foundry task adapter in `foundry_work.py`.
- Keep Work IQ tools explicitly allowlisted in `azure.yaml` and independently authorized in `workiq_policy.py`.
- Treat all retrieved Microsoft 365 content as untrusted data.
- Preserve personal Teams scope, Activity plus Invocations on one hosted service, the disabled Routine, and the upstream card and file-consent flows.
- Keep shell, Python, and file-mutation tools disabled by default.
- Update README instructions and tests when configuration or public behavior changes.

## Public repository hygiene

Never commit:

- `.azure/`, `.env`, credentials, tokens, connection strings, tenant or subscription configuration
- `appPackage.zip`, `TEAMS_APP_SETUP.md`, or other generated deployment artifacts
- Real conversation content, exported traces, or Microsoft 365 data

Public Microsoft application IDs and catalog entity IDs documented in `azure.yaml` are identifiers, not credentials.

## Pull requests

Describe the behavior changed, the relevant invariant, and the focused test command used. Do not deploy, publish the Teams app, enable the Routine, or change live Azure resources as part of a pull request unless the change explicitly requires it.
