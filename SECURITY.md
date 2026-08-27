# Security Policy

## Reporting a vulnerability

Do not open a public Issue for a suspected vulnerability. Use this repository's private vulnerability reporting form:

https://github.com/iceginqxf-rgb/local-code-cli-orchestrator/security/advisories/new

Include the affected command/profile, impact, reproduction steps, and any proposed mitigation. Avoid including real credentials, proprietary prompts, or session logs from a production repository.

## Scope reminder

The orchestrator avoids shell execution and does not install CLIs, but a selected coding agent still runs with the current user's permissions. Product-specific autonomy flags and the code being executed remain part of the user's trust boundary.
