# Local Code CLI Orchestrator

[![CI](https://github.com/iceginqxf-rgb/local-code-cli-orchestrator/actions/workflows/ci.yml/badge.svg)](https://github.com/iceginqxf-rgb/local-code-cli-orchestrator/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Delegate coding work to installed terminal agents through durable background sessions, then review the actual Git diff, PR, and CI evidence independently.

The orchestrator is designed for a Lead Agent that delegates implementation to another code CLI. The implementation CLI may edit, validate, commit, push, and open or update a PR when authorized. It does not install missing tools, continuously watch logs, merge the PR, or claim that the work is accepted or released.

## Why

- Background-first execution with resumable session metadata.
- Complete execution logs and bounded result summaries.
- Exact working-directory and concurrent-session safeguards.
- No automatic CLI installation or silent product substitution.
- Registry-driven adapters for multiple coding agents.
- PR-only delivery boundary with one-shot GitHub/CI evidence.
- Lead review checkpoints that cannot merge or deploy.
- Exact commit matching and clean-worktree checks before `merge_ready`.

```text
Lead Agent
  -> durable background session
  -> installed Code CLI implements and opens/updates PR
  -> GitHub CI runs independently
  -> Lead Agent reviews Git + PR + CI evidence
  -> changes_requested | awaiting_ci | blocked | merge_ready
```

## Supported CLIs

The built-in registry currently includes:

| Support | CLIs |
|---|---|
| Ready adapter | Codex, Claude Code, GitHub Copilot CLI, Antigravity, Grok Build, Factory Droid, Kimi Code, Pi, OpenCode, Gemini CLI, Qwen Code, Aider, Goose |
| Provisional | Kiro CLI |
| Detection only | Cursor Agent, Cursor launcher, Amp, Crush, Cline, Roo Code, Auggie |

`ready` means a non-interactive command pattern is defined. CLI interfaces can change between product versions, so scan the installed version and use a registry override when its current syntax differs. Detection-only profiles are never launched automatically.

## Requirements

- Python 3.10 or newer.
- Git for repository snapshots and PR delivery mode.
- At least one supported Code CLI already installed and authenticated.
- Optional: GitHub CLI (`gh`) for one-shot PR/CI review evidence.
- Optional: tmux only when live observation is explicitly useful.

The orchestrator itself has no third-party Python runtime dependencies.

## Install as a Codex skill

```bash
git clone https://github.com/iceginqxf-rgb/local-code-cli-orchestrator.git \
  ~/.codex/skills/local-code-cli-orchestrator
```

It can also run as a standalone utility from any checkout.

## Quick start

Check an exact CLI without installing anything:

```bash
./scripts/scan-code-clis.sh --cli droid
```

Prepare a self-contained prompt in a private or gitignored location, then start a durable background session:

```bash
./scripts/start-code-cli-session.sh \
  --cli droid \
  --cwd /absolute/path/to/project \
  --prompt /absolute/path/to/task-prompt.md \
  --session issue-123-droid-01
```

The command returns immediately. Later, inspect status once and generate the review bundle:

```bash
python3 scripts/code-cli-session.py status --session issue-123-droid-01
python3 scripts/code-cli-session.py review --session issue-123-droid-01
```

Read a bounded log tail only when diagnosing a failure:

```bash
python3 scripts/code-cli-session.py logs \
  --session issue-123-droid-01 \
  --tail 120
```

## GitHub PR delivery mode

Use one Delivery Issue, feature branch, linked worktree, and PR:

```bash
python3 scripts/code-cli-session.py start \
  --delivery-mode pr \
  --issue '#123' \
  --base-branch main \
  --branch feat/issue-123-auth-timeout \
  --change-type modify \
  --module auth \
  --cli droid \
  --cwd /absolute/path/to/repo-wt-123-auth-timeout \
  --prompt /absolute/path/to/issue-123.md \
  --session issue-123-droid-01
```

PR mode appends a mandatory boundary to the implementation prompt:

- validate locally, commit, push, and create/update the PR when authorized;
- stop after the PR triggers CI;
- do not wait, watch, or poll CI;
- never merge, push the default branch, deploy after merge, or claim acceptance/release.

`review` captures GitHub and CI once. If checks are pending, leave the session at `awaiting_ci` and return later for another single snapshot. Once the Lead Agent has independently reviewed the diff and gates, record the conclusion without merging:

```bash
python3 scripts/code-cli-session.py checkpoint \
  --session issue-123-droid-01 \
  --decision merge-ready \
  --summary "Reviewed commit matches PR head; required CI passed."
```

Other decisions are `changes-requested` and `blocked`. A later `review` marks the previous Lead checkpoint stale until a new conclusion is recorded.

See [the complete session lifecycle](references/session-lifecycle.md) and [the PR delivery boundary](references/ai-native-delivery.md).

## Durable artifacts

By default, sessions live under:

```text
~/.codex/local-code-cli-orchestrator/sessions/<session-id>/
```

Each session can contain the immutable task contract, source/effective prompt snapshots, status, full execution log, result, before/after/review Git evidence, a one-shot GitHub/CI snapshot, and Lead review history. Files are created with private user permissions.

Session directories may contain proprietary code context and CLI output. Do not commit or publish them.

## Custom adapters

Built-in adapters live in [`scripts/cli-profiles.json`](scripts/cli-profiles.json). Keep local changes outside the checkout in:

```text
~/.codex/local-code-cli-orchestrator/cli-profiles.json
```

You may also pass an explicit registry with the global `--registry` option. See [CLI profiles](references/cli-profiles.md) for the schema and verification rules.

## Development

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q scripts tests
python3 -m json.tool scripts/cli-profiles.json >/dev/null
bash -n scripts/scan-code-clis.sh scripts/start-code-cli-session.sh
```

See [CONTRIBUTING.md](CONTRIBUTING.md) before changing an adapter or delivery boundary.

## Security

The runner never invokes a shell for a selected Code CLI, but the selected agent still operates with the permissions of the current user. Review its product-specific autonomy flags, use isolated worktrees, and never place credentials in prompts or command arguments.

See [SECURITY.md](SECURITY.md) for vulnerability reporting.

## License

MIT. See [LICENSE](LICENSE).

This project is independent and is not endorsed by the vendors of the supported CLIs.
