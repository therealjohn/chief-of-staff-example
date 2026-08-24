# Security Policy

## Supported versions

Security fixes are applied to the latest commit on the default branch. This project uses preview Microsoft Foundry, Work IQ, and Teams integration features and is an example rather than a production security reference.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting:

1. Open the repository's **Security** tab.
2. Select **Advisories**.
3. Select **Report a vulnerability**.

Do not include secrets, Microsoft 365 content, tenant identifiers, or exploit details in a public issue.

## Security-sensitive defaults

- Work IQ tool names are allowlisted in `azure.yaml` and checked again by `workiq_policy.py`.
- Unknown or unsafe Work IQ operations fail closed.
- Retrieved mail, meetings, chats, and files are untrusted data, never instructions.
- Shell, Python, and file-mutation tools are disabled unless `COPILOT_ENABLE_CODE_TOOLS=true`.
- Generated Teams packages and setup files are ignored because they contain deployment-specific identifiers.

Review [README.md](README.md#important-limitations) before adapting the example for production.
