---
name: local-code-cli-orchestrator
description: Delegate coding work to installed terminal code agents through durable background sessions, logs, results, and independent Lead Agent review. Use when an agent should run or supervise another local coding CLI, including PR-only GitHub delivery; use tmux only when live observation is explicitly useful.
metadata:
  short-description: Run local code CLIs as reviewable background sessions
---

# Local Code CLI Orchestrator

Delegate implementation to an installed local coding CLI while the Lead Agent owns task framing and independent acceptance. Default to a detached, non-interactive run that leaves a durable session; do not continuously monitor it.

## Invariants

- Resolve and confirm the intended project directory before launch.
- Read the project's `AGENTS.md`, `CLAUDE.md`, or equivalent guide when present.
- Use a dedicated session and a self-contained prompt file for every task.
- Do not run two active sessions in the same Git worktree by default. Use a separate worktree or explicit concurrency only when the user intends it.
- Detect the exact requested CLI before launch. Never install a missing CLI automatically.
- If the requested CLI is missing, tell the user and ask whether they want it installed. Do not invoke package managers, download scripts, or on-demand wrappers without explicit approval.
- Do not add unsafe permission bypasses implicitly. Pass product-specific autonomy flags only when the task authorizes them.
- Treat CLI output as evidence, not acceptance. Inspect the actual diff and run project checks independently.
- In GitHub delivery mode, the external Code CLI may create or update the feature PR but never merge it, push the default branch, close the Delivery Issue, clean its worktree, or claim Engineering Done/Accepted/Released.
- Preserve existing user changes and stop if the CLI works in the wrong directory.

## Choose a mode

Resolve `<skill-dir>` to the directory containing this `SKILL.md`. Use the durable runner for ordinary work:

```bash
python3 <skill-dir>/scripts/code-cli-session.py \
  scan --cli <profile>

python3 <skill-dir>/scripts/code-cli-session.py \
  start \
  --cli <profile> \
  --cwd <absolute-project-directory> \
  --prompt <prompt-file> \
  --session <stable-session-id>
```

`start` defaults to background mode and returns immediately. Do not tail or poll after a successful launch. Record the session ID for a later status check and review.

Use `--mode tmux` only when the user asks to watch live execution or detached background processes are not durable on that host. Use `--mode foreground` only for adapter debugging or short deliberate runs.

For full commands, artifact layout, timeouts, cancellation, and lifecycle rules, read [references/session-lifecycle.md](references/session-lifecycle.md) before launching or reviewing a session.

## GitHub PR delivery

For a Delivery Issue, read [references/ai-native-delivery.md](references/ai-native-delivery.md) before dispatch. Treat the repository's own contribution guide and workflow profile as authoritative when they define stricter or project-specific gates.

The Lead Agent creates or selects the exact Issue branch and linked worktree, then launches with `--delivery-mode pr` plus the Issue, base branch, feature branch, change types, and affected modules. The runner rejects the base branch and, absent an explicit project-profile exception, the main worktree. It appends a mandatory PR-only/non-merge boundary to the effective prompt.

The external Implementation Agent runs local validation, commits, pushes, opens or updates the PR so remote CI is triggered, then exits (or reports a precise blocker). It reports the CI state already available but must not wait, watch, or poll for CI. The Lead Agent independently reviews the baseline-to-HEAD diff, uncommitted changes, Issue scope, change-type gates, documentation, validation, and a later one-shot PR/CI snapshot.

If CI is still queued or running, record `awaiting_ci` and end the active review turn. Resume later from a fresh one-shot snapshot or a host-provided event/automation; do not keep the Code CLI session alive and do not continuously tail logs. After review, persist `changes_requested`, `merge_ready`, or `blocked` with the runner's `checkpoint` command. This records the Lead Agent conclusion only and never merges.

After review, the Lead Agent may merge only when the user explicitly authorizes it or the project workflow profile already grants that authority after stated gates. Otherwise report `merge-ready` and stop. Normal CD begins after merge to the default branch (or an authorized tag/release); PR preview deployments are optional and are not production release. A merge means Engineering Done only; post-merge deployment, product acceptance, and release remain separate.

## CLI selection

The registry covers Codex, Claude Code, GitHub Copilot, Antigravity, Grok Build, Factory Droid, Kimi Code, Pi, OpenCode, Gemini CLI, Qwen Code, Aider, Goose, and several detection-only CLIs. It supports aliases and user overrides without changing the runner.

Run `scan --cli <name>` for the user's exact choice. Do not silently substitute another product when a named CLI is missing. If the user did not choose a CLI, scan installed `ready` profiles and recommend one based on task needs.

Read [references/cli-profiles.md](references/cli-profiles.md) only when selecting, adding, or correcting an adapter. A `detect-only` profile must not run until its current non-interactive syntax has been verified and added through a registry override.

## Prompt contract

Keep the source prompt in a project-approved gitignored agent directory, or in a private location outside the repository, for example:

```text
~/.codex/local-code-cli-orchestrator/prompts/<task-id>.md
```

The runner snapshots it into the session. Do not leave an untracked orchestration prompt in the delivery worktree and do not commit it in the PR unless the project explicitly treats that prompt as maintained documentation.

Include the resolved working directory, exact outcome, relevant project instructions, allowed scope, forbidden changes, likely files, validation commands, UI/manual expectations, and required final report. For PR delivery also include the Delivery Issue contract, add/modify/delete declarations, affected modules, branch/base, PR contract, CI expectations, and risk/rollback. Make it self-contained; the CLI must not require this chat history.

Do not put credentials in prompts or arguments. Session logs may contain proprietary code and tool output; the runner stores them with private user permissions.

## Review checkpoint

When review is requested, check status once:

```bash
python3 <skill-dir>/scripts/code-cli-session.py \
  status --session <session-id>
```

If terminal, generate the review bundle:

```bash
python3 <skill-dir>/scripts/code-cli-session.py \
  review --session <session-id>
```

Then independently inspect `git status`, the stored base-to-HEAD PR diff, and project-specific tests, type checks, lint, build, browser, or manual validation. For GitHub delivery, query the PR and required CI directly. Do not mark work complete solely because the external CLI exited successfully or claimed success, and do not conflate Engineering Done, Accepted, and Released.

In PR mode, `review` also stores one read-only GitHub/CI snapshot; it never uses watch mode or polls. When the diff and gates have been reviewed, persist the Lead Agent decision:

```bash
python3 <skill-dir>/scripts/code-cli-session.py \
  checkpoint \
  --session <session-id> \
  --decision merge-ready \
  --summary-file <lead-review-summary>
```

Use `changes-requested` or `blocked` when appropriate. `merge-ready` requires a completed session, clean worktree, exact reviewed commit matching the open non-draft PR head, a mergeable PR, and passing required CI evidence. A project-authorized CI exception must be explicit and recorded; it cannot replace proof that the PR exists or matches the reviewed commit. The checkpoint still performs no merge or deployment.
