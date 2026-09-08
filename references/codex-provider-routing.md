# Codex alternate-provider routing

Use this reference only when the Lead Agent is Codex and implementation should run through a cheaper model or a different provider. Sol remains responsible for task framing, risk decisions, diff review, validation, and acceptance.

## Decision table

| Rank for bounded same-turn work | Path | Best fit | Main limit |
|---|---|---|---|
| 1 | External Code CLI + `monitor_luna` | Ordinary implementation expected to finish while the current turn remains alive | The turn, monitor, or app can still be interrupted |
| 2 | Durable external Code CLI + later review | Long, offline, interruptible, or cross-session work | Completion is not automatically surfaced unless a host event/heartbeat resumes the task |
| 3 | Sol model-driven polling | One exceptional immediate diagnostic only | Repeated Lead-model turns waste tokens and attention |

For a run likely to outlive the current active turn, rank the durable path first. Do not keep a model alive merely to preserve immediacy.

## Path 1 — external CLI with `monitor_luna`

This is the default for bounded, interactive Codex work.

1. The Sol Lead writes the complete implementation contract and launches the selected external Code CLI with `start` in background mode.
2. Spawn exactly one `monitor_luna` custom agent with no inherited conversation turns. Its agent file pins `gpt-5.6-luna`, low reasoning effort, read-only access, and the notifier-only boundary; do not try to override its effort.
3. Give the monitor only the exact session ID, runner path, state root if non-default, and bounded timeout. It starts exactly one waiter process:

   ```bash
   python3 <skill-dir>/scripts/code-cli-session.py \
     wait --session <session-id> --timeout <bounded-seconds> --json
   ```

4. The Sol Lead uses Codex's native agent wait mechanism. When the monitor returns, Sol reads the terminal payload, creates the review bundle, inspects the actual changes, and runs independent checks.

The monitor is a notifier, not an implementer or reviewer. It must not inspect the repository, tail logs, modify files, start another agent, or interpret success claims. If the terminal tool yields before the process exits, the monitor may only resume or wait on that same terminal process; it must not launch another `wait`, `status`, or `logs` command. A failed, cancelled, timed-out, or stale implementation is still a terminal notification and must be handed back to Sol.

Recommended monitor prompt:

```text
You are a completion monitor, not an implementer or reviewer.
Start exactly this one deterministic wait command and make no other substantive tool calls:
<exact command including --session, --timeout, and --json>

Do not inspect source files, tail logs, edit anything, start agents, or poll with repeated model turns.
If the terminal tool yields an ongoing session handle, only continue waiting on that same handle; never start the command again or call status/logs.
When the command exits, return its complete JSON payload verbatim. A terminal failure is still a completed monitoring assignment. If the wait itself reaches its bound, report that fact and the current state; do not start another wait.
```

Why this works:

- `monitor_luna` is an ordinary same-provider Codex subagent, using OpenAI alongside the Lead; it requires no initialization workaround and does not route implementation to another provider.
- Status-file checking happens inside one deterministic Python process. Sleeping and file checks do not require Luna to reason about progress. Some terminal tool wrappers yield long-running commands and require the monitor to continue the same process handle; those sparse continuations may use small Luna turns, but they do not wake Sol or create a model-driven status loop.
- The Lead waits on the monitor through Codex orchestration and resumes only when it receives a result or the bounded wait needs attention.

This is not a permanent OS event subscription. It depends on the active task, Codex runtime, and monitor staying alive. Use a realistic bound. If it expires, do not chain fresh monitors indefinitely; preserve the session and switch to the durable later-review path.

If `monitor_luna` is unavailable on another host, use `default` with explicit `gpt-5.6-luna`, low effort, and no inherited turns. If neither role nor per-spawn model selection is available, fall back to the durable path rather than silently using Sol as the watcher. A pure waiter never needs medium or higher effort; use a different role for work that requires interpretation.

## Retired route — native cross-provider role dispatch

Verified on 2026-09-08 against Codex 0.153.4 and a local `explorer` test with `fork_turns="none"`, without Luna initialization. The role requested Grok through a custom provider; the child kept `openai`, selected the Grok model name, and sent the request to the ChatGPT Codex endpoint, which rejected it.

This is a shared role-configuration restriction, not a V2-only initialization problem:

- [AgentRoleOverrides](https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/core/src/agent/role.rs#L37) includes `model` but excludes `model_provider` and `model_providers`.
- [The authority-preservation test](https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/core/src/agent/role_tests.rs#L393) supplies another provider, then asserts that the model changes while the provider remains the parent's.
- Both V1 and V2 spawn handlers use the same `apply_spawn_agent_role` / `apply_role_to_config` path.

Do not offer the historical Luna-first / V1 initialization sequence as a working route. Do not register external-provider native `worker` or `explorer` roles, change the Lead's global provider, or silently substitute a provider. Native Codex roles stay on OpenAI in this setup. Use a separately selected external CLI for alternate-provider implementation; its availability and authentication still require normal preflight checks.

This restriction concerns changing provider through a child role, not whether an independent top-level CLI can use a custom provider. Reconsider the retired route only after an explicit user request, evidence that a newer implementation supports it, and a minimal test verifying actual provider, model, endpoint, and successful output. A role name or model self-report is insufficient.

## Path 2 — durable launch and later review

Launch the external Code CLI in background mode, return the session ID, and end the active turn. Later, resume with one `status` call and, once terminal, one `review` call. This is the most resilient route for runs lasting hours, app restarts, host disconnection, quota windows, or work that should survive the current Codex turn.

When the product supports thread heartbeats or automations, one may resume the task later. Prefer a coarse, bounded follow-up over frequent cron-style polling. A scheduled check should inspect status once, review only if terminal, and stop or reschedule deliberately if still active.

## Path 3 — Lead-model polling

Do not ask Sol to repeatedly call `status`, read logs, or reason about unchanged progress. That consumes Lead-model turns without improving the implementation. One immediate status check for diagnosis or a single deterministic blocking `wait` invoked directly by the Lead can be token-light, but the latter ties up the Lead turn and provides no cheap-agent isolation. Prefer Path 1 when same-turn notification matters.

## Completion and review boundaries

- `wait` observes only the local implementation process. It never watches GitHub Actions, deployment, or another remote workflow.
- The wait operation returns zero for every observed terminal state because notification succeeded. Read `state`, `exit_code`, `result_file`, and `execution_log` to determine what happened.
- If `wait_timed_out` is true, the implementation may still be running. Do not stop it unless the user requested cancellation or the task contract requires termination.
- Never accept work from the monitor's message or the external CLI's result alone. Generate the review bundle, inspect the diff, and run the relevant project gates.
- In PR delivery, the implementation agent exits after creating or updating the PR. CI is checked once later; it is not kept alive by any watcher.
