# Durable session lifecycle

Resolve `<skill-dir>` to the skill's installed directory, then use the unified runner:

```bash
python3 <skill-dir>/scripts/code-cli-session.py <command>
```

The runner uses only the Python standard library. It never installs a CLI and never invokes a shell to run the selected agent.

## 1. Detect the requested CLI

Check the exact profile before preparing a launch:

```bash
python3 <skill-dir>/scripts/code-cli-session.py \
  scan --cli droid
```

If it is missing, stop. Tell the user that it is not installed and ask whether they want it installed. Do not run package managers, installers, curl scripts, or download-on-demand wrappers without explicit approval.

Run `scan` without `--cli` to see the full registry. The states mean:

- `ready`: a non-interactive command pattern is documented and can be launched.
- `provisional`: the pattern is known but must be checked against the installed version; starting it requires `--allow-provisional`.
- `detect-only`: the command can be recognized, but the skill intentionally has no execution pattern yet.

## 2. Prepare a self-contained prompt

Keep the source prompt in a project-approved gitignored agent directory or a private location outside the repository. The runner copies it into the durable session. Do not leave an untracked orchestration prompt in the delivery worktree or commit it to the PR unless the project explicitly requires that document. Include:

- Exact working directory and goal.
- Required project instructions and files to read.
- Allowed scope and forbidden changes.
- Permission/autonomy expectations for this particular CLI.
- Validation commands and any browser/manual checks.
- A concise final report request.

Do not put API keys or other secrets in the prompt or CLI arguments.

For an AI-native GitHub Delivery Issue, read [ai-native-delivery.md](ai-native-delivery.md) and launch PR mode from its dedicated linked worktree:

```bash
python3 <skill-dir>/scripts/code-cli-session.py \
  start \
  --delivery-mode pr \
  --issue '#123' \
  --base-branch main \
  --branch feat/issue-123-auth-timeout \
  --change-type modify \
  --module auth \
  --cli droid \
  --cwd /absolute/repo-wt-123-auth-timeout \
  --prompt /absolute/private-prompts/issue-123.md \
  --session issue-123-droid-01 \
  --extra='--auto medium'
```

PR mode validates the declared feature branch, resolves and records the base commit, and requires a linked worktree unless the project workflow profile explicitly justifies `--allow-main-worktree`. It appends the executor boundary to `prompt.md`: implement, validate, commit, push, and create/update the PR when authorized, but never wait/watch/poll for CI, merge, deploy after merge, or claim a higher completion layer.

## 3. Start in the background

Background mode is the default:

```bash
python3 <skill-dir>/scripts/code-cli-session.py \
  start \
  --cli droid \
  --cwd /absolute/project/path \
  --prompt /absolute/private-prompts/task.md \
  --session task-droid-01 \
  --model gpt-5.6-sol \
  --effort high \
  --extra='--auto medium'
```

CLI-specific flags remain explicit. The generic runner does not translate a universal autonomy flag because the meaning differs between products. It also does not add unsafe approval-bypass flags.

The default timeout is 7,200 seconds. Set a different bounded timeout when the task warrants it. Use `--timeout 0` only when an unbounded run is genuinely intended.

The runner refuses a second active session in the same Git worktree. Prefer a separate worktree for parallel implementation. Use `--allow-concurrent` only when overlapping access is deliberate and safe.

After launch, return the session ID and session directory. Do not keep the turn open by continuously tailing or polling.

## 4. Check once, then review

Read status when review is requested or when a later workflow resumes:

```bash
python3 <skill-dir>/scripts/code-cli-session.py \
  status --session task-droid-01
```

When terminal, build the review bundle:

```bash
python3 <skill-dir>/scripts/code-cli-session.py \
  review --session task-droid-01
```

The review bundle gathers the CLI result, execution metadata, and Git snapshots. In PR mode it includes the reproducible `base_commit...HEAD` diff summary/name-status and commits, so committed work remains visible even when the worktree is clean. It also uses the installed, authenticated GitHub CLI for one read-only PR/CI snapshot. It never enables watch mode or polls. If GitHub CLI is missing or unavailable, the bundle records that fact and performs no installation.

The bundle does not accept the work. Independently inspect the full diff, Delivery Issue gates, project validation, actual PR, and CI evidence. If CI is pending, end the active review and resume later from another single snapshot rather than tailing it in real time.

After the Lead Agent reaches a conclusion, persist it without merging:

```bash
python3 <skill-dir>/scripts/code-cli-session.py \
  checkpoint \
  --session task-droid-01 \
  --decision merge-ready \
  --summary-file /absolute/path/to/lead-review-summary.md
```

Other decisions are `changes-requested` and `blocked`. `merge-ready` verifies that the implementation completed, the worktree is clean, the exact reviewed commit matches the PR head, the PR matches the delivery contract and is open/non-draft/mergeable, and required CI passed. If a project workflow profile explicitly authorizes a CI exception, record its reason with `--gate-exception`; an exception cannot replace PR/commit identity evidence. The checkpoint stores the Lead decision but cannot merge, deploy, accept, or release.

A later `review` refreshes the evidence and marks any prior Lead checkpoint stale. Record a new checkpoint after examining the new snapshot; this prevents an old `merge_ready` conclusion from surviving a new commit, rerun, PR closure, or changed CI state.

Read a bounded log tail only when diagnosing a failure:

```bash
python3 <skill-dir>/scripts/code-cli-session.py \
  logs --session task-droid-01 --tail 120
```

## Optional tmux mode

Use tmux only when the user explicitly wants live observation or when the host cannot preserve a detached background process:

```bash
python3 <skill-dir>/scripts/code-cli-session.py \
  start \
  --mode tmux \
  --cli grok \
  --cwd /absolute/project/path \
  --prompt /absolute/private-prompts/task.md \
  --session task-grok-live
```

The durable artifacts are identical in background and tmux modes.

## Session artifacts

The default session root is:

```text
~/.codex/local-code-cli-orchestrator/sessions/<session-id>/
```

Each session contains:

```text
session.json       Immutable task, delivery contract, adapter, command, and path metadata
status.json        Queued/running/terminal state and process IDs
prompt-source.md   Unmodified snapshot of the submitted prompt
prompt.md          Effective prompt, including the PR-only boundary when applicable
execution.log      Complete combined CLI stdout/stderr
runner.log         Supervisor lifecycle and adapter errors
result.md          Native final result when supported; otherwise a bounded synthesis
git-before.json    Git state before launch
git-after.json     Git state immediately after worker exit
git-review.json    Fresh Git state when review is requested
git-checkpoint.json
                   Fresh Git state when the Lead decision is recorded
github-review.json One-shot PR and CI snapshot; never a live watcher
review.md          Consolidated review bundle
lead-review.json   Latest Lead Agent conclusion; never a merge action
lead-review-history.json
                   Prior Lead checkpoints for this session
```

Session files are created with private user permissions. The command preview replaces the prompt body with a placeholder, but CLI-specific extra arguments are recorded; never pass secrets as arguments.

## Stop an exact session

Only stop when the user requests cancellation or the task's stopping condition is met:

```bash
python3 <skill-dir>/scripts/code-cli-session.py \
  stop --session task-droid-01
```

The runner targets the recorded child process group rather than a broad command-name match.
