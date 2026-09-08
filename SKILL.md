---
name: local-code-cli-orchestrator
description: Delegate coding work to local terminal code agents through durable background sessions, token-efficient completion notification, logs, results, and independent Lead Agent review. Use when Codex should run or supervise another installed coding CLI or alternate-provider model, including PR-only implementation in an AI-native GitHub delivery loop; use tmux only when live observation is explicitly useful.
metadata:
  short-description: Run code CLIs with cheap completion monitoring
---

# Local Code CLI Orchestrator

Delegate implementation to an installed local coding CLI while Codex owns task framing and independent acceptance. Default to a detached, non-interactive run that leaves a durable session. When Codex needs an in-turn completion signal, prefer the custom `monitor_luna` role plus the runner's deterministic `wait` command; never spend Lead-model turns polling.

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

## Codex alternate-provider routing

When the Lead Agent is Codex and another provider or cheaper model should do the implementation, read [references/codex-provider-routing.md](references/codex-provider-routing.md) before dispatch. Choose by expected lifecycle, not only by nominal priority:

1. For bounded work that should return to the same active turn, prefer an external Code CLI plus the custom `monitor_luna` role. That role is pinned to `gpt-5.6-luna`, low effort, and read-only access. Spawn it without inherited conversation turns; it runs one silent `wait` command and reports the terminal payload. Sol waits natively, then performs the review. This is the default Codex path and does not require a Luna initialization turn.
2. For long, interruptible, or cross-session work, launch the durable Code CLI and return. Resume from one later `status`/`review` check or a host heartbeat/automation. This is less immediate but more resilient than keeping a turn alive.
3. Do not make Sol repeatedly call `status` or tail logs. One immediate diagnostic check is fine; model-driven polling is the last-resort path.

Native cross-provider role dispatch is unavailable in the verified Codex 0.153.4 implementation: V1 and V2 share role handling that preserves the parent provider. Do not configure an external provider on native `worker` / `explorer`, recommend disabling V2, or use Luna initialization as a workaround. Keep native Codex roles on OpenAI; use an explicitly selected external CLI for another provider without changing the Lead's global provider configuration. `monitor_luna` remains a same-provider completion notifier. See the routing reference for evidence and revalidation criteria.

These priorities reverse for work expected to outlive the active turn: durable launch and later review becomes the first choice. Never use a local process waiter to watch remote PR CI; take one CI snapshot and resume from an event or later check.

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

`start` defaults to background mode and returns immediately. Do not tail logs or perform model-driven polling after launch. Either record the session ID for a later review, or—only for bounded same-turn work—spawn `monitor_luna` with no inherited conversation turns and give it exactly one deterministic waiter process:

```bash
python3 <skill-dir>/scripts/code-cli-session.py \
  wait --session <session-id> --timeout <bounded-seconds> --json
```

The `wait` command emits no progress while active. It returns zero after observing any terminal implementation state, including failure; inspect the returned `state` and never confuse successful notification with successful implementation.

If `monitor_luna` is unavailable in another Codex installation, fall back to a `default` subagent with explicit `gpt-5.6-luna` and low effort. Never silently use the Sol Lead as the watcher.

Use `--mode tmux` only when the user asks to watch live execution or detached background processes are not durable on that host. Use `--mode foreground` only for adapter debugging or short deliberate runs.

For full commands, artifact layout, timeouts, cancellation, and lifecycle rules, read [references/session-lifecycle.md](references/session-lifecycle.md) before launching or reviewing a session.

## AI-native GitHub delivery

For a Delivery Issue, follow the project's contribution guide and workflow profile; load `ai-native-build` when that skill is installed and applicable. Read [references/ai-native-delivery.md](references/ai-native-delivery.md) before dispatch.

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

When review is requested, use the terminal payload from `wait` or check status once:

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
