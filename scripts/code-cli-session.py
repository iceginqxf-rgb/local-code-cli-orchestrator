#!/usr/bin/env python3
"""Durable, background-first orchestration for local coding CLIs.

The runner intentionally does not install CLIs, grant unsafe permissions, or
continuously monitor work. It creates an auditable session and returns.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from typing import Any


SCRIPT_PATH = Path(__file__).resolve()
SKILL_DIR = SCRIPT_PATH.parent.parent
BUILTIN_REGISTRY = SCRIPT_PATH.parent / "cli-profiles.json"
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
ANSI_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
TERMINAL_STATES = {"completed", "failed", "timed_out", "cancelled", "stale"}


class OrchestratorError(RuntimeError):
    def __init__(self, message: str, exit_code: int = 1):
        super().__init__(message)
        self.exit_code = exit_code


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def default_state_root() -> Path:
    configured = os.environ.get("CODEX_HOME")
    codex_root = Path(configured).expanduser() if configured else Path.home() / ".codex"
    return codex_root / "local-code-cli-orchestrator" / "sessions"


def atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(value, encoding="utf-8")
    os.replace(tmp, path)


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise OrchestratorError(f"Required file does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise OrchestratorError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise OrchestratorError(f"Expected a JSON object in {path}")
    return data


def merge_profile_file(
    profiles: dict[str, Any], path: Path, sources: list[str]
) -> None:
    if not path.exists():
        return
    payload = read_json(path)
    if payload.get("schema_version") != 1:
        raise OrchestratorError(f"Unsupported registry schema in {path}")
    additions = payload.get("profiles")
    if not isinstance(additions, dict):
        raise OrchestratorError(f"Registry has no profiles object: {path}")
    for profile_id, profile in additions.items():
        if not isinstance(profile, dict):
            raise OrchestratorError(
                f"Profile {profile_id!r} in {path} must be an object"
            )
        profiles[profile_id] = profile
    sources.append(str(path.resolve()))


def load_profiles(explicit_registry: str | None) -> tuple[dict[str, Any], list[str]]:
    profiles: dict[str, Any] = {}
    sources: list[str] = []
    merge_profile_file(profiles, BUILTIN_REGISTRY, sources)
    user_registry = default_state_root().parent / "cli-profiles.json"
    merge_profile_file(profiles, user_registry, sources)
    if explicit_registry:
        merge_profile_file(
            profiles, Path(explicit_registry).expanduser().resolve(), sources
        )
    return profiles, sources


def resolve_profile(
    profiles: dict[str, Any], requested: str
) -> tuple[str, dict[str, Any]]:
    if requested in profiles:
        return requested, profiles[requested]
    for profile_id, profile in profiles.items():
        aliases = profile.get("aliases", [])
        if requested in aliases:
            return profile_id, profile
    known = ", ".join(sorted(profiles))
    raise OrchestratorError(
        f"Unknown CLI profile {requested!r}. Known profiles: {known}", 2
    )


def expand_detect_path(value: str) -> Path:
    return Path(value).expanduser()


def locate_executable(profile: dict[str, Any]) -> tuple[str | None, str]:
    commands = profile.get("commands", [])
    if not isinstance(commands, list) or not commands:
        return None, "profile has no command detector"
    executable = None
    for command in commands:
        if isinstance(command, str):
            executable = shutil.which(command)
            if executable:
                break
    paths = profile.get("detect_paths", [])
    existing_paths = [
        str(expand_detect_path(p)) for p in paths if expand_detect_path(p).exists()
    ]
    if profile.get("require_detect_path") and not existing_paths:
        return None, "required local payload was not found"
    if not executable:
        return None, "command is not on PATH"
    return executable, "installed"


def profile_version(profile: dict[str, Any], executable: str, cwd: Path) -> str | None:
    version_args = profile.get("version_args", [])
    if not version_args:
        return None
    mapping = {"executable": executable}
    command = render_tokens(profile.get("command", ["{executable}"]), mapping)
    command += render_tokens(version_args, mapping)
    code, output = capture(command, cwd, timeout=5)
    if code == 0 and output:
        return output.splitlines()[0][:240]
    return None


def render_tokens(tokens: list[Any], mapping: dict[str, str]) -> list[str]:
    rendered: list[str] = []
    for raw in tokens:
        if not isinstance(raw, str):
            raise OrchestratorError("CLI profile command tokens must be strings")
        value = raw
        for key, replacement in mapping.items():
            value = value.replace("{" + key + "}", replacement)
        rendered.append(value)
    return rendered


def build_command(
    profile: dict[str, Any],
    executable: str,
    prompt_file: Path,
    result_file: Path,
    cwd: Path,
    model: str | None,
    effort: str | None,
    extra_args: list[str],
    preview: bool = False,
) -> tuple[list[str], str | None]:
    support = profile.get("support", "detect-only")
    if support == "detect-only":
        raise OrchestratorError(
            "This profile is detection-only and has no verified background adapter"
        )
    prompt_config = profile.get("prompt")
    if not isinstance(prompt_config, dict):
        raise OrchestratorError("CLI profile has no prompt transport")
    prompt_text = prompt_file.read_text(encoding="utf-8")
    prompt_display = f"<prompt:{prompt_file.name}>" if preview else prompt_text
    mapping = {
        "executable": executable,
        "prompt": prompt_display,
        "prompt_file": str(prompt_file),
        "result_file": str(result_file),
        "cwd": str(cwd),
        "model": model or "",
        "effort": effort or "",
    }
    command = render_tokens(profile.get("command", ["{executable}"]), mapping)
    command += render_tokens(profile.get("base_args", []), mapping)
    if model:
        model_args = profile.get("model_args", [])
        if not model_args:
            raise OrchestratorError(
                f"Profile does not define model selection, but --model={model!r} was supplied"
            )
        command += render_tokens(model_args, mapping)
    if effort:
        effort_args = profile.get("effort_args", [])
        if not effort_args:
            raise OrchestratorError(
                f"Profile does not define effort selection, but --effort={effort!r} was supplied"
            )
        command += render_tokens(effort_args, mapping)
    command += extra_args
    prompt_args = prompt_config.get("args", [])
    command += render_tokens(prompt_args, mapping)
    transport = prompt_config.get("transport")
    if transport == "stdin":
        return command, None if preview else prompt_text
    if transport in {"arg", "file"}:
        if (
            transport == "arg"
            and not preview
            and len(prompt_text.encode("utf-8")) > 200_000
        ):
            raise OrchestratorError(
                "Prompt is too large for this profile's argument transport. Add a file/stdin adapter in the registry."
            )
        return command, None
    raise OrchestratorError(f"Unsupported prompt transport: {transport!r}")


def capture(args: list[str], cwd: Path, timeout: int = 15) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            args,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=False,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
        return proc.returncode, proc.stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)


def clipped(value: str, limit: int = 8_000) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "\n[Output truncated.]"


def parse_json_output(value: str) -> Any | None:
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None


def summarize_checks(records: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {
        "pass": 0,
        "fail": 0,
        "pending": 0,
        "skipping": 0,
        "cancel": 0,
        "unknown": 0,
    }
    for record in records:
        bucket = str(record.get("bucket", "unknown")).lower()
        if bucket not in counts:
            bucket = "unknown"
        counts[bucket] += 1
    total = sum(counts.values())
    if total == 0:
        state = "none"
    elif counts["fail"] or counts["cancel"]:
        state = "failed"
    elif counts["pending"]:
        state = "pending"
    elif counts["unknown"]:
        state = "unknown"
    else:
        state = "passed"
    return {"state": state, "total": total, "counts": counts}


def github_checks_snapshot(
    gh: str, cwd: Path, target: str, required: bool
) -> dict[str, Any]:
    command = [
        gh,
        "pr",
        "checks",
        target,
        "--json",
        "name,state,bucket,link,workflow",
    ]
    if required:
        command.append("--required")
    exit_code, output = capture(command, cwd, timeout=15)
    parsed = parse_json_output(output)
    records = (
        [item for item in parsed if isinstance(item, dict)]
        if isinstance(parsed, list)
        else None
    )
    no_checks_text = output.lower()
    if records is None and (
        "no checks reported" in no_checks_text
        or "no required checks reported" in no_checks_text
    ):
        records = []
    if records is None:
        return {
            "query_state": "unavailable",
            "exit_code": exit_code,
            "error": clipped(output),
            "records": [],
            "summary": {"state": "unknown", "total": 0},
        }
    return {
        "query_state": "ok",
        "exit_code": exit_code,
        "records": records,
        "summary": summarize_checks(records),
    }


def github_workflow_state(snapshot: dict[str, Any]) -> str | None:
    if snapshot.get("snapshot_state") != "captured":
        return "awaiting_pr_evidence"
    pr = snapshot.get("pr", {})
    state = str(pr.get("state", "")).upper()
    if state == "MERGED":
        return "pr_merged_requires_lead_confirmation"
    if state == "CLOSED":
        return "pr_closed"
    if state != "OPEN":
        return "awaiting_pr_evidence"
    if pr.get("isDraft"):
        return "pr_draft"
    gate_state = snapshot.get("ci_gate", {}).get("state")
    if gate_state == "pending":
        return "awaiting_ci"
    if gate_state == "failed":
        return "ci_failed"
    if gate_state == "passed":
        return "awaiting_lead_review"
    if gate_state == "none":
        return "awaiting_lead_review_no_ci"
    return "awaiting_ci_evidence"


def github_snapshot(cwd: Path, delivery: dict[str, Any]) -> dict[str, Any]:
    """Capture one read-only PR/CI snapshot; never watch or poll."""
    target = str(delivery.get("pr_url") or delivery.get("branch") or "").strip()
    snapshot: dict[str, Any] = {
        "captured_at": utc_now(),
        "capture_mode": "one_shot",
        "watch_or_poll": False,
        "target": target,
    }
    gh = shutil.which("gh")
    if not gh:
        snapshot.update(
            {
                "snapshot_state": "unavailable",
                "error": "GitHub CLI is not installed or not available on PATH. No installation was attempted.",
            }
        )
        snapshot["workflow_state_hint"] = github_workflow_state(snapshot)
        return snapshot
    if not target:
        snapshot.update(
            {
                "snapshot_state": "unavailable",
                "error": "No PR URL or feature branch was recorded for lookup.",
            }
        )
        snapshot["workflow_state_hint"] = github_workflow_state(snapshot)
        return snapshot
    fields = (
        "number,url,title,state,isDraft,mergeable,mergeStateStatus,reviewDecision,"
        "headRefName,headRefOid,baseRefName,baseRefOid,updatedAt,mergedAt"
    )
    exit_code, output = capture(
        [gh, "pr", "view", target, "--json", fields], cwd, timeout=15
    )
    parsed = parse_json_output(output)
    if exit_code != 0 or not isinstance(parsed, dict):
        snapshot.update(
            {
                "snapshot_state": "unavailable",
                "pr_view_exit_code": exit_code,
                "error": clipped(output),
            }
        )
        snapshot["workflow_state_hint"] = github_workflow_state(snapshot)
        return snapshot
    all_checks = github_checks_snapshot(gh, cwd, target, required=False)
    required_checks = github_checks_snapshot(gh, cwd, target, required=True)
    required_summary = required_checks.get("summary", {})
    all_summary = all_checks.get("summary", {})
    required_known = required_checks.get("query_state") == "ok"
    required_configured = required_known and required_summary.get("total", 0) > 0
    if required_configured:
        gate_basis = "required_checks"
        gate_state = required_summary.get("state", "unknown")
    elif required_known and all_checks.get("query_state") == "ok":
        gate_basis = "all_checks_fallback"
        gate_state = all_summary.get("state", "unknown")
    else:
        gate_basis = "unknown"
        gate_state = "unknown"
    snapshot.update(
        {
            "snapshot_state": "captured",
            "pr_view_exit_code": exit_code,
            "pr": parsed,
            "checks": {"all": all_checks, "required": required_checks},
            "ci_gate": {
                "state": gate_state,
                "basis": gate_basis,
                "required_checks_configured": required_configured,
                "required_query_known": required_known,
            },
        }
    )
    snapshot["workflow_state_hint"] = github_workflow_state(snapshot)
    return snapshot


def git_snapshot(cwd: Path, base_commit: str | None = None) -> dict[str, Any]:
    code, root = capture(["git", "rev-parse", "--show-toplevel"], cwd)
    if code != 0:
        return {"is_git_repository": False, "captured_at": utc_now()}
    repo = Path(root).resolve()
    head_code, head = capture(["git", "rev-parse", "HEAD"], repo)
    branch_code, branch = capture(["git", "branch", "--show-current"], repo)
    _, status = capture(["git", "status", "--short"], repo)
    _, diff_stat = capture(["git", "diff", "--stat"], repo)
    _, staged_stat = capture(["git", "diff", "--cached", "--stat"], repo)
    diff_check_code, diff_check = capture(["git", "diff", "--check"], repo)
    snapshot: dict[str, Any] = {
        "is_git_repository": True,
        "captured_at": utc_now(),
        "root": str(repo),
        "head": head if head_code == 0 else None,
        "branch": branch if branch_code == 0 else None,
        "status_short": status,
        "diff_stat": diff_stat,
        "staged_diff_stat": staged_stat,
        "diff_check_exit_code": diff_check_code,
        "diff_check_output": diff_check,
    }
    if base_commit:
        range_spec = f"{base_commit}...HEAD"
        pr_check_code, pr_check = capture(["git", "diff", "--check", range_spec], repo)
        _, pr_diff_stat = capture(["git", "diff", "--stat", range_spec], repo)
        _, pr_name_status = capture(["git", "diff", "--name-status", range_spec], repo)
        _, commits = capture(["git", "log", "--oneline", f"{base_commit}..HEAD"], repo)
        snapshot.update(
            {
                "base_commit": base_commit,
                "pr_diff_range": range_spec,
                "pr_diff_stat": pr_diff_stat,
                "pr_name_status": pr_name_status,
                "pr_diff_check_exit_code": pr_check_code,
                "pr_diff_check_output": pr_check,
                "commits_since_base": commits,
            }
        )
    return snapshot


def resolve_git_internal_path(repo: Path, value: str) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (repo / path).resolve()


def is_linked_worktree(repo: Path) -> bool:
    git_dir_code, git_dir = capture(["git", "rev-parse", "--git-dir"], repo)
    common_code, common_dir = capture(["git", "rev-parse", "--git-common-dir"], repo)
    if git_dir_code != 0 or common_code != 0:
        return False
    return resolve_git_internal_path(repo, git_dir) != resolve_git_internal_path(
        repo, common_dir
    )


def resolve_base(repo: Path, base_branch: str) -> tuple[str, str]:
    candidates = [base_branch]
    if "/" not in base_branch:
        candidates = [f"origin/{base_branch}", base_branch]
    for candidate in candidates:
        code, commit = capture(["git", "rev-parse", "--verify", candidate], repo)
        if code == 0 and commit:
            return candidate, commit
    raise OrchestratorError(
        f"Could not resolve base branch {base_branch!r}. Fetch the repository or correct --base-branch.",
        2,
    )


def build_delivery_context(args: argparse.Namespace, cwd: Path) -> dict[str, Any]:
    if args.delivery_mode == "local":
        return {"mode": "local"}
    missing: list[str] = []
    for option, value in [
        ("--issue", args.issue),
        ("--base-branch", args.base_branch),
        ("--branch", args.branch),
    ]:
        if not value:
            missing.append(option)
    if not args.change_type:
        missing.append("--change-type")
    if not args.module:
        missing.append("--module")
    if missing:
        raise OrchestratorError("PR delivery mode requires: " + ", ".join(missing), 2)
    code, root = capture(["git", "rev-parse", "--show-toplevel"], cwd)
    if code != 0:
        raise OrchestratorError("PR delivery mode requires a Git repository", 2)
    repo = Path(root).resolve()
    branch_code, actual_branch = capture(["git", "branch", "--show-current"], repo)
    if branch_code != 0 or not actual_branch:
        raise OrchestratorError(
            "PR delivery mode requires a non-detached feature branch", 2
        )
    if actual_branch != args.branch:
        raise OrchestratorError(
            f"Current branch is {actual_branch!r}, but --branch declared {args.branch!r}",
            2,
        )
    if (
        actual_branch == args.base_branch
        or actual_branch == f"origin/{args.base_branch}"
    ):
        raise OrchestratorError(
            "PR delivery mode cannot run on the declared base branch", 2
        )
    linked = is_linked_worktree(repo)
    if not linked and not args.allow_main_worktree:
        raise OrchestratorError(
            "PR delivery mode requires a dedicated linked worktree. Create one for the Delivery Issue, "
            "or use --allow-main-worktree only for a project-profile exception.",
            2,
        )
    base_ref, base_commit = resolve_base(repo, args.base_branch)
    return {
        "mode": "pr",
        "issue": args.issue,
        "change_types": list(dict.fromkeys(args.change_type)),
        "modules": list(dict.fromkeys(args.module)),
        "worktree": str(repo),
        "branch": actual_branch,
        "base_branch": args.base_branch,
        "base_ref": base_ref,
        "base_commit": base_commit,
        "pr_url": args.pr_url,
        "linked_worktree": linked,
        "executor_boundary": "open_or_update_pr_without_merge",
        "ci_observation": "lead_one_shot_no_executor_wait_or_poll",
        "post_merge_delivery": "outside_implementation_session",
    }


def delivery_prompt_addendum(delivery: dict[str, Any]) -> str:
    if delivery.get("mode") != "pr":
        return ""
    change_types = ", ".join(delivery["change_types"])
    modules = ", ".join(delivery["modules"])
    return f"""
## Orchestrator delivery boundary (mandatory)

You are the Implementation Agent for Delivery Issue {delivery["issue"]}.

- Work only in `{delivery["worktree"]}` on branch `{delivery["branch"]}` against base `{delivery["base_ref"]}` (`{delivery["base_commit"]}`).
- Declared change types: {change_types}. Declared modules: {modules}. Do not expand beyond them; report a scope change instead.
- Implement, validate, commit, push the feature branch, and create or update its PR when the task authorizes those GitHub writes.
- Never merge the PR, push to the default branch, close the Delivery Issue manually, delete the branch/worktree, or mark Engineering Done, Accepted, or Released.
- Stop after the PR is open/updated and remote CI has been triggered, or report the exact blocker. Report the current CI state if readily available, but do not wait, watch, or poll for CI and do not keep this implementation process alive for it.
- The Lead Agent separately reviews the diff and captures later CI evidence. It owns the merge decision under user/project authorization; post-merge deployment, product acceptance, and release are outside this implementation session.
- Final report must include Issue, branch/worktree, PR URL/state, change-type gates, exact validation commands/results, CI state, risk/rollback, follow-ups, and any scope exception. Report the workflow state as `In Review` or `Blocked`, never as accepted or released.
""".strip()


def validate_session_id(value: str) -> str:
    if not SESSION_ID_RE.fullmatch(value):
        raise OrchestratorError(
            "Session ID must be 1-80 characters using letters, numbers, dot, underscore, or hyphen",
            2,
        )
    return value


def generated_session_id(profile_id: str) -> str:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    safe_profile = re.sub(r"[^A-Za-z0-9._-]+", "-", profile_id).strip("-") or "cli"
    return f"{stamp}-{safe_profile}-{uuid.uuid4().hex[:6]}"


def session_dir(state_root: Path, session_id: str) -> Path:
    return state_root / validate_session_id(session_id)


def load_session(
    state_root: Path, session_id: str
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    directory = session_dir(state_root, session_id)
    metadata = read_json(directory / "session.json")
    status_path = directory / "status.json"
    status = read_json(status_path) if status_path.exists() else {"state": "unknown"}
    return directory, metadata, status


def update_status(directory: Path, **changes: Any) -> dict[str, Any]:
    path = directory / "status.json"
    current = read_json(path) if path.exists() else {}
    current.update(changes)
    current["updated_at"] = utc_now()
    atomic_write_json(path, current)
    return current


def pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def timestamp_is_recent(value: Any, max_age_seconds: int = 60) -> bool:
    if not isinstance(value, str):
        return False
    try:
        stamp = dt.datetime.fromisoformat(value)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=dt.timezone.utc)
        age = dt.datetime.now(dt.timezone.utc) - stamp.astimezone(dt.timezone.utc)
        return age.total_seconds() <= max_age_seconds
    except ValueError:
        return False


def inferred_state(status: dict[str, Any]) -> str:
    state = str(status.get("state", "unknown"))
    if state in TERMINAL_STATES:
        return state
    child_alive = pid_alive(status.get("child_pid"))
    worker_alive = pid_alive(status.get("worker_pid"))
    if state == "queued" and timestamp_is_recent(status.get("updated_at")):
        return state
    if state in {"queued", "running", "stop_requested"} and not (
        child_alive or worker_alive
    ):
        return "stale"
    return state


def workspace_identity(cwd: Path) -> str:
    code, root = capture(["git", "rev-parse", "--show-toplevel"], cwd)
    return str(Path(root).resolve()) if code == 0 else str(cwd.resolve())


def active_workspace_sessions(state_root: Path, workspace_key: str) -> list[str]:
    conflicts: list[str] = []
    if not state_root.exists():
        return conflicts
    for directory in state_root.iterdir():
        metadata_path = directory / "session.json"
        status_path = directory / "status.json"
        if (
            not directory.is_dir()
            or not metadata_path.exists()
            or not status_path.exists()
        ):
            continue
        try:
            metadata = read_json(metadata_path)
            status = read_json(status_path)
        except OrchestratorError:
            continue
        existing_key = metadata.get("workspace_key") or metadata.get("cwd")
        if existing_key and str(Path(existing_key).resolve()) != workspace_key:
            continue
        if inferred_state(status) not in TERMINAL_STATES:
            conflicts.append(str(metadata.get("session_id", directory.name)))
    return sorted(conflicts)


def terminate_child(proc: subprocess.Popen[Any], grace_seconds: int = 8) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    proc.wait(timeout=grace_seconds)


def synthesize_result(directory: Path, exit_code: int) -> None:
    result_path = directory / "result.md"
    if result_path.exists() and result_path.stat().st_size > 0:
        return
    log_path = directory / "execution.log"
    if not log_path.exists():
        content = "(The CLI produced no execution log.)\n"
    else:
        raw = log_path.read_bytes()
        truncated = len(raw) > 262_144
        if truncated:
            raw = raw[-262_144:]
        content = raw.decode("utf-8", errors="replace")
        content = ANSI_RE.sub("", content)
        if truncated:
            content = (
                "[Earlier output omitted; see execution.log for the complete log.]\n\n"
                + content
            )
    header = f"# CLI result\n\nExit code: `{exit_code}`\n\n"
    atomic_write_text(result_path, header + content.rstrip() + "\n")


def pump_output(source: Any, log_handle: Any, tee: bool) -> None:
    while True:
        chunk = source.read(8192)
        if not chunk:
            break
        log_handle.write(chunk)
        log_handle.flush()
        if tee:
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()


def run_worker(directory: Path, tee: bool = False) -> int:
    metadata = read_json(directory / "session.json")
    profile = metadata["profile_snapshot"]
    cwd = Path(metadata["cwd"])
    delivery = metadata.get("delivery", {"mode": "local"})
    prompt_file = directory / "prompt.md"
    result_file = directory / "result.md"
    command, stdin_text = build_command(
        profile=profile,
        executable=metadata["executable"],
        prompt_file=prompt_file,
        result_file=result_file,
        cwd=cwd,
        model=metadata.get("model"),
        effort=metadata.get("effort"),
        extra_args=metadata.get("extra_args", []),
    )
    started_monotonic = time.monotonic()
    update_status(
        directory,
        state="running",
        workflow_state="implementation_running"
        if delivery.get("mode") == "pr"
        else "local_running",
        worker_pid=os.getpid(),
        started_at=utc_now(),
    )
    log_path = directory / "execution.log"
    runner_log = directory / "runner.log"
    exit_code = 1
    timed_out = False
    child: subprocess.Popen[Any] | None = None
    with runner_log.open("a", encoding="utf-8") as runner:
        runner.write(f"[{utc_now()}] worker started\n")
        runner.write(f"command: {metadata['command_preview']}\n")
        runner.flush()
        try:
            with log_path.open("ab") as log_handle:
                if tee:
                    child = subprocess.Popen(
                        command,
                        cwd=str(cwd),
                        stdin=subprocess.PIPE
                        if stdin_text is not None
                        else subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                    assert child.stdout is not None
                    pump = threading.Thread(
                        target=pump_output,
                        args=(child.stdout, log_handle, True),
                        daemon=True,
                    )
                    pump.start()
                else:
                    child = subprocess.Popen(
                        command,
                        cwd=str(cwd),
                        stdin=subprocess.PIPE
                        if stdin_text is not None
                        else subprocess.DEVNULL,
                        stdout=log_handle,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                    pump = None
                update_status(directory, child_pid=child.pid)
                if stdin_text is not None:
                    assert child.stdin is not None
                    child.stdin.write(stdin_text.encode("utf-8"))
                    child.stdin.close()
                timeout = int(metadata.get("timeout_seconds", 0))
                try:
                    exit_code = child.wait(timeout=timeout if timeout > 0 else None)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    runner.write(
                        f"[{utc_now()}] timeout reached; terminating child process group\n"
                    )
                    runner.flush()
                    terminate_child(child)
                    exit_code = 124
                if pump is not None:
                    pump.join(timeout=10)
        except FileNotFoundError as exc:
            exit_code = 127
            runner.write(f"[{utc_now()}] executable not found: {exc}\n")
        except (
            Exception
        ) as exc:  # keep the durable status even on adapter/runtime faults
            exit_code = 1
            runner.write(f"[{utc_now()}] worker error: {type(exc).__name__}: {exc}\n")
        duration = round(time.monotonic() - started_monotonic, 3)
        stop_requested = (directory / "stop-requested").exists()
        if timed_out:
            state = "timed_out"
        elif stop_requested:
            state = "cancelled"
        elif exit_code == 0:
            state = "completed"
        else:
            state = "failed"
        synthesize_result(directory, exit_code)
        if delivery.get("mode") == "pr":
            if state == "completed":
                workflow_state = "awaiting_lead_review"
            elif state in {"timed_out", "cancelled"}:
                workflow_state = "implementation_interrupted"
            else:
                workflow_state = "implementation_failed"
        else:
            workflow_state = f"local_{state}"
        atomic_write_json(
            directory / "git-after.json",
            git_snapshot(cwd, delivery.get("base_commit")),
        )
        update_status(
            directory,
            state=state,
            workflow_state=workflow_state,
            exit_code=exit_code,
            finished_at=utc_now(),
            duration_seconds=duration,
            result_file=str(result_file),
            execution_log=str(log_path),
        )
        runner.write(
            f"[{utc_now()}] worker finished: state={state} exit_code={exit_code}\n"
        )
    return exit_code


def start_background(directory: Path, cwd: Path, state_root: Path) -> int:
    runner_log = (directory / "runner.log").open("ab")
    command = [
        sys.executable,
        str(SCRIPT_PATH),
        "--state-root",
        str(state_root),
        "_worker",
        "--session",
        directory.name,
    ]
    try:
        worker = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=runner_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        runner_log.close()
    update_status(directory, worker_pid=worker.pid)
    return worker.pid


def start_tmux(directory: Path, cwd: Path, state_root: Path) -> str:
    tmux = shutil.which("tmux")
    if not tmux:
        raise OrchestratorError("tmux mode was requested, but tmux is not installed")
    tmux_name = ("codecli-" + directory.name)[:90]
    command = [
        sys.executable,
        str(SCRIPT_PATH),
        "--state-root",
        str(state_root),
        "_worker",
        "--session",
        directory.name,
        "--tee",
    ]
    proc = subprocess.run(
        [
            tmux,
            "new-session",
            "-d",
            "-s",
            tmux_name,
            "-c",
            str(cwd),
            shlex.join(command),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise OrchestratorError(f"Could not start tmux session: {proc.stdout.strip()}")
    update_status(directory, tmux_session=tmux_name)
    return tmux_name


def parse_extra_args(args: argparse.Namespace) -> list[str]:
    extras = list(args.extra_arg or [])
    if args.extra:
        extras.extend(shlex.split(args.extra))
    return extras


def command_start(args: argparse.Namespace) -> int:
    if args.timeout < 0:
        raise OrchestratorError(
            "--timeout must be zero or a positive number of seconds", 2
        )
    profiles, sources = load_profiles(args.registry)
    profile_id, profile = resolve_profile(profiles, args.cli)
    support = profile.get("support", "detect-only")
    if support == "detect-only":
        raise OrchestratorError(
            f"{profile.get('label', profile_id)} is recognized, but its background adapter is not verified. "
            "Use a reviewed registry override instead of guessing its flags.",
            2,
        )
    if support == "provisional" and not args.allow_provisional:
        raise OrchestratorError(
            f"Profile {profile_id!r} is provisional. Re-run with --allow-provisional after checking this CLI version's help.",
            2,
        )
    executable, reason = locate_executable(profile)
    if not executable:
        raise OrchestratorError(
            f"{profile.get('label', profile_id)} is not installed or not available on PATH ({reason}). "
            "No installation was attempted. Tell the user it is missing and ask whether they want it installed.",
            3,
        )
    cwd = Path(args.cwd).expanduser().resolve()
    if not cwd.is_dir():
        raise OrchestratorError(f"Working directory does not exist: {cwd}", 2)
    prompt_source = Path(args.prompt).expanduser()
    if not prompt_source.is_absolute():
        prompt_source = cwd / prompt_source
    prompt_source = prompt_source.resolve()
    if not prompt_source.is_file():
        raise OrchestratorError(f"Prompt file does not exist: {prompt_source}", 2)
    delivery = build_delivery_context(args, cwd)
    source_prompt_text = prompt_source.read_text(encoding="utf-8")
    addendum = delivery_prompt_addendum(delivery)
    effective_prompt_text = (
        source_prompt_text.rstrip() + "\n\n" + addendum + "\n"
        if addendum
        else source_prompt_text
    )
    prompt_transport = profile.get("prompt", {}).get("transport")
    if (
        prompt_transport == "arg"
        and len(effective_prompt_text.encode("utf-8")) > 200_000
    ):
        raise OrchestratorError(
            "Effective prompt is too large for this profile's argument transport. "
            "Use a file/stdin adapter.",
            2,
        )
    state_root = Path(args.state_root).expanduser().resolve()
    state_root.mkdir(parents=True, exist_ok=True)
    session_id = (
        validate_session_id(args.session)
        if args.session
        else generated_session_id(profile_id)
    )
    directory = session_dir(state_root, session_id)
    if directory.exists():
        raise OrchestratorError(f"Session already exists: {session_id}", 2)
    workspace_key = workspace_identity(cwd)
    if not args.allow_concurrent:
        conflicts = active_workspace_sessions(state_root, workspace_key)
        if conflicts:
            raise OrchestratorError(
                "An active session already targets this workspace: "
                + ", ".join(conflicts)
                + ". Wait for it to finish, use a separate worktree, or explicitly pass --allow-concurrent.",
                5,
            )
    extras = parse_extra_args(args)
    result_file = directory / "result.md"
    build_command(
        profile=profile,
        executable=executable,
        prompt_file=prompt_source,
        result_file=result_file,
        cwd=cwd,
        model=args.model,
        effort=args.effort,
        extra_args=extras,
        preview=True,
    )
    directory.mkdir(mode=0o700, parents=True)
    source_prompt_file = directory / "prompt-source.md"
    prompt_file = directory / "prompt.md"
    atomic_write_text(source_prompt_file, source_prompt_text)
    atomic_write_text(prompt_file, effective_prompt_text)
    command_preview, _ = build_command(
        profile=profile,
        executable=executable,
        prompt_file=prompt_file,
        result_file=result_file,
        cwd=cwd,
        model=args.model,
        effort=args.effort,
        extra_args=extras,
        preview=True,
    )
    metadata = {
        "schema_version": 1,
        "session_id": session_id,
        "created_at": utc_now(),
        "cwd": str(cwd),
        "workspace_key": workspace_key,
        "prompt_source": str(prompt_source),
        "prompt_source_snapshot": str(source_prompt_file),
        "prompt_snapshot": str(prompt_file),
        "delivery": delivery,
        "profile_id": profile_id,
        "profile_label": profile.get("label", profile_id),
        "profile_support": support,
        "profile_snapshot": profile,
        "registry_sources": sources,
        "executable": executable,
        "cli_version": profile_version(profile, executable, cwd),
        "model": args.model,
        "effort": args.effort,
        "extra_args": extras,
        "mode": args.mode,
        "timeout_seconds": args.timeout,
        "command_preview": shlex.join(command_preview),
        "artifacts": {
            "status": str(directory / "status.json"),
            "prompt_source": str(source_prompt_file),
            "prompt_effective": str(prompt_file),
            "execution_log": str(directory / "execution.log"),
            "runner_log": str(directory / "runner.log"),
            "result": str(result_file),
            "review": str(directory / "review.md"),
            "git_checkpoint": str(directory / "git-checkpoint.json"),
            "github_review": str(directory / "github-review.json"),
            "lead_review": str(directory / "lead-review.json"),
            "lead_review_history": str(directory / "lead-review-history.json"),
        },
    }
    atomic_write_json(directory / "session.json", metadata)
    atomic_write_json(
        directory / "git-before.json",
        git_snapshot(cwd, delivery.get("base_commit")),
    )
    atomic_write_json(
        directory / "status.json",
        {
            "state": "queued",
            "workflow_state": "implementation_queued"
            if delivery.get("mode") == "pr"
            else "local_queued",
            "created_at": metadata["created_at"],
            "updated_at": utc_now(),
        },
    )
    worker_pid = None
    tmux_name = None
    if args.mode == "background":
        worker_pid = start_background(directory, cwd, state_root)
    elif args.mode == "tmux":
        tmux_name = start_tmux(directory, cwd, state_root)
    else:
        run_worker(directory, tee=True)
    response = {
        "session_id": session_id,
        "state": inferred_state(read_json(directory / "status.json")),
        "mode": args.mode,
        "delivery_mode": delivery.get("mode"),
        "delivery": delivery,
        "session_dir": str(directory),
        "status_file": str(directory / "status.json"),
        "result_file": str(result_file),
        "execution_log": str(directory / "execution.log"),
        "worker_pid": worker_pid,
        "tmux_session": tmux_name,
    }
    if args.json:
        print(json.dumps(response, ensure_ascii=False, indent=2))
    else:
        print(f"Session: {session_id}")
        print(f"Mode: {args.mode}")
        print(f"Delivery mode: {delivery.get('mode')}")
        print(f"State: {response['state']}")
        print(f"Session directory: {directory}")
        if worker_pid:
            print(f"Background worker PID: {worker_pid}")
        if tmux_name:
            print(
                f"Attach only if live observation is needed: tmux attach -t {tmux_name}"
            )
        print(
            "No continuous monitoring is required. Check status once when review is requested."
        )
    return 0


def scan_record(profile_id: str, profile: dict[str, Any]) -> dict[str, Any]:
    executable, reason = locate_executable(profile)
    version = profile_version(profile, executable, Path.cwd()) if executable else None
    return {
        "id": profile_id,
        "label": profile.get("label", profile_id),
        "support": profile.get("support", "detect-only"),
        "installed": bool(executable),
        "executable": executable,
        "version": version,
        "reason": reason,
        "aliases": profile.get("aliases", []),
    }


def command_scan(args: argparse.Namespace) -> int:
    profiles, _ = load_profiles(args.registry)
    if args.cli:
        profile_id, profile = resolve_profile(profiles, args.cli)
        records = [scan_record(profile_id, profile)]
    else:
        records = [
            scan_record(profile_id, profiles[profile_id])
            for profile_id in sorted(profiles)
        ]
    if args.json:
        print(json.dumps(records, ensure_ascii=False, indent=2))
    else:
        print(f"{'ID':18} {'STATE':12} {'SUPPORT':12} DETAILS")
        for record in records:
            state = "installed" if record["installed"] else "missing"
            details = record["executable"] or record["reason"]
            if record["version"]:
                details += f" ({record['version']})"
            print(
                f"{record['id'][:18]:18} {state:12} {record['support'][:12]:12} {details}"
            )
        if args.cli and not records[0]["installed"]:
            print(
                "\nNo installation was attempted. Tell the user it is missing and ask whether they want it installed."
            )
    return 3 if args.cli and not records[0]["installed"] else 0


def status_payload(
    directory: Path, metadata: dict[str, Any], status: dict[str, Any]
) -> dict[str, Any]:
    payload = dict(status)
    payload["state"] = inferred_state(status)
    payload["session_id"] = metadata.get("session_id", directory.name)
    payload["cli"] = metadata.get("profile_id")
    payload["cwd"] = metadata.get("cwd")
    payload["delivery"] = metadata.get("delivery", {"mode": "local"})
    payload["session_dir"] = str(directory)
    payload["result_file"] = str(directory / "result.md")
    payload["execution_log"] = str(directory / "execution.log")
    payload["review_file"] = str(directory / "review.md")
    payload["github_review_file"] = str(directory / "github-review.json")
    payload["lead_review_file"] = str(directory / "lead-review.json")
    return payload


def command_status(args: argparse.Namespace) -> int:
    state_root = Path(args.state_root).expanduser().resolve()
    directory, metadata, status = load_session(state_root, args.session)
    payload = status_payload(directory, metadata, status)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"Session: {payload['session_id']}")
        print(f"CLI: {payload['cli']}")
        print(f"State: {payload['state']}")
        print(f"Workflow state: {payload.get('workflow_state', 'not available')}")
        print(f"Delivery mode: {payload['delivery'].get('mode', 'local')}")
        if payload.get("exit_code") is not None:
            print(f"Exit code: {payload['exit_code']}")
        if payload.get("duration_seconds") is not None:
            print(f"Duration: {payload['duration_seconds']}s")
        print(f"Result: {payload['result_file']}")
        print(f"Log: {payload['execution_log']}")
        if payload["delivery"].get("mode") == "pr":
            print(f"Review: {payload['review_file']}")
            print(f"Lead checkpoint: {payload['lead_review_file']}")
    return 0 if payload["state"] != "stale" else 4


def command_list(args: argparse.Namespace) -> int:
    state_root = Path(args.state_root).expanduser().resolve()
    records: list[dict[str, Any]] = []
    if state_root.exists():
        directories = sorted(
            (
                p
                for p in state_root.iterdir()
                if p.is_dir() and (p / "session.json").exists()
            ),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        cwd_filter = str(Path(args.cwd).expanduser().resolve()) if args.cwd else None
        for directory in directories:
            metadata = read_json(directory / "session.json")
            if cwd_filter and metadata.get("cwd") != cwd_filter:
                continue
            status = (
                read_json(directory / "status.json")
                if (directory / "status.json").exists()
                else {}
            )
            records.append(status_payload(directory, metadata, status))
            if len(records) >= args.limit:
                break
    if args.json:
        print(json.dumps(records, ensure_ascii=False, indent=2))
    else:
        print(f"{'SESSION':38} {'STATE':12} {'CLI':14} CWD")
        for record in records:
            print(
                f"{record['session_id'][:38]:38} {record['state'][:12]:12} "
                f"{str(record.get('cli', ''))[:14]:14} {record.get('cwd', '')}"
            )
    return 0


def tail_text(path: Path, max_bytes: int = 131_072) -> str:
    if not path.exists():
        return "(not available)"
    raw = path.read_bytes()
    truncated = len(raw) > max_bytes
    if truncated:
        raw = raw[-max_bytes:]
    value = ANSI_RE.sub("", raw.decode("utf-8", errors="replace")).strip()
    if truncated:
        value = "[Earlier content omitted.]\n\n" + value
    return value or "(empty)"


def fenced(value: str) -> str:
    return "```text\n" + value.replace("```", "` ` `") + "\n```"


def command_review(args: argparse.Namespace) -> int:
    state_root = Path(args.state_root).expanduser().resolve()
    directory, metadata, status = load_session(state_root, args.session)
    state = inferred_state(status)
    cwd = Path(metadata["cwd"])
    delivery = metadata.get("delivery", {"mode": "local"})
    base_commit = delivery.get("base_commit")
    before = (
        read_json(directory / "git-before.json")
        if (directory / "git-before.json").exists()
        else {}
    )
    after = (
        read_json(directory / "git-after.json")
        if (directory / "git-after.json").exists()
        else git_snapshot(cwd, base_commit)
    )
    current = git_snapshot(cwd, base_commit)
    atomic_write_json(directory / "git-review.json", current)
    github_evidence: dict[str, Any] = {
        "snapshot_state": "not_applicable",
        "capture_mode": "one_shot",
        "watch_or_poll": False,
    }
    if delivery.get("mode") == "pr":
        if args.skip_github:
            github_evidence = {
                "captured_at": utc_now(),
                "snapshot_state": "skipped",
                "capture_mode": "one_shot",
                "watch_or_poll": False,
                "reason": "Skipped explicitly for this review.",
            }
        else:
            github_evidence = github_snapshot(cwd, delivery)
        atomic_write_json(directory / "github-review.json", github_evidence)
        workflow_hint = github_evidence.get("workflow_state_hint")
        status_changes: dict[str, Any] = {
            "reviewed_at": utc_now(),
            "lead_review_stale": bool(status.get("lead_review_decision")),
        }
        if state == "completed" and workflow_hint:
            status_changes["workflow_state"] = workflow_hint
            status_changes["github_checked_at"] = github_evidence.get("captured_at")
        status = update_status(directory, **status_changes)
    result = tail_text(directory / "result.md")
    sections = [
        "# Code CLI session review bundle",
        "",
        f"- Session: `{metadata['session_id']}`",
        f"- CLI: `{metadata['profile_id']}`",
        f"- CLI version: `{metadata.get('cli_version') or 'not reported'}`",
        f"- State: `{state}`",
        f"- Workflow state: `{status.get('workflow_state', 'not available')}`",
        f"- Delivery mode: `{delivery.get('mode', 'local')}`",
        f"- Exit code: `{status.get('exit_code', 'not available')}`",
        f"- Working directory: `{metadata['cwd']}`",
        f"- Command: `{metadata['command_preview'].replace('`', '')}`",
        f"- Full execution log: `{directory / 'execution.log'}`",
        "",
        "## Delivery contract",
        "",
        fenced(json.dumps(delivery, ensure_ascii=False, indent=2)),
        "",
        "## CLI-reported result",
        "",
        fenced(result),
        "",
        "## Git snapshot before",
        "",
        fenced(json.dumps(before, ensure_ascii=False, indent=2)),
        "",
        "## Git snapshot after worker exit",
        "",
        fenced(json.dumps(after, ensure_ascii=False, indent=2)),
        "",
        "## Git snapshot at review time",
        "",
        fenced(json.dumps(current, ensure_ascii=False, indent=2)),
        "",
        "## GitHub PR and CI snapshot",
        "",
        fenced(json.dumps(github_evidence, ensure_ascii=False, indent=2)),
        "",
        "## Independent review checkpoint",
        "",
        "The orchestrated CLI result is evidence, not acceptance. Inspect the full base-to-HEAD diff and uncommitted changes, verify the Delivery Issue scope and add/modify/delete gates, and run project validation independently.",
        "",
        "For PR delivery, this bundle captures GitHub and CI once; it never uses watch mode or polling. If checks are pending, record `awaiting_ci`, end the active review turn, and resume later from a fresh one-shot snapshot. After the diff and all engineering gates pass, persist the Lead Agent conclusion with `checkpoint`.",
        "",
        "The Implementation Agent never merges. The Lead Agent may merge only under explicit user authorization or a project workflow profile that pre-authorizes merge after all engineering gates pass. Merge means Engineering Done only; post-merge CD, product acceptance, and release remain separate decisions.",
        "",
    ]
    review_path = directory / "review.md"
    atomic_write_text(review_path, "\n".join(sections))
    payload = {
        "session_id": metadata["session_id"],
        "state": state,
        "exit_code": status.get("exit_code"),
        "review_file": str(review_path),
        "result_file": str(directory / "result.md"),
        "execution_log": str(directory / "execution.log"),
        "github_review": str(directory / "github-review.json")
        if delivery.get("mode") == "pr"
        else None,
        "cwd": str(cwd),
        "delivery": delivery,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"Review bundle: {review_path}")
        print(f"State: {state}; exit code: {status.get('exit_code', 'not available')}")
        if state not in TERMINAL_STATES:
            print("The session is still active; this review bundle is partial.")
    return 0


def checkpoint_summary(args: argparse.Namespace, cwd: Path) -> str:
    if args.summary is not None:
        value = args.summary.strip()
    else:
        path = Path(args.summary_file).expanduser()
        if not path.is_absolute():
            path = cwd / path
        try:
            value = path.resolve().read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise OrchestratorError(f"Could not read checkpoint summary: {exc}", 2)
    if not value:
        raise OrchestratorError("Checkpoint summary must not be empty", 2)
    return value


def validate_merge_ready_checkpoint(
    state: str,
    delivery: dict[str, Any],
    reviewed_git: dict[str, Any],
    current_git: dict[str, Any],
    github_evidence: dict[str, Any],
    gate_exception: str | None,
) -> None:
    if state != "completed":
        raise OrchestratorError(
            "A merge-ready checkpoint requires a successfully completed implementation session",
            2,
        )
    if github_evidence.get("snapshot_state") != "captured":
        raise OrchestratorError(
            "A merge-ready checkpoint requires a current GitHub PR snapshot. Run review again; "
            "a CI gate exception cannot replace evidence that the PR exists.",
            2,
        )
    if not reviewed_git.get("is_git_repository") or not current_git.get(
        "is_git_repository"
    ):
        raise OrchestratorError("Merge-ready requires a valid Git review snapshot", 2)
    if reviewed_git.get("branch") != delivery.get("branch") or current_git.get(
        "branch"
    ) != delivery.get("branch"):
        raise OrchestratorError(
            "The reviewed local branch does not match the delivery contract", 2
        )
    if (
        str(reviewed_git.get("status_short", "")).strip()
        or str(current_git.get("status_short", "")).strip()
    ):
        raise OrchestratorError(
            "Merge-ready requires a clean delivery worktree with no uncommitted changes",
            2,
        )
    if current_git.get("head") != reviewed_git.get("head"):
        raise OrchestratorError(
            "The delivery commit changed after the Lead review. Run review again before marking merge-ready.",
            2,
        )
    pr = github_evidence.get("pr", {})
    if str(pr.get("state", "")).upper() != "OPEN":
        raise OrchestratorError("Merge-ready requires an open PR", 2)
    if pr.get("isDraft"):
        raise OrchestratorError("Merge-ready cannot be recorded for a draft PR", 2)
    if pr.get("headRefName") != delivery.get("branch"):
        raise OrchestratorError(
            "The reviewed PR head branch does not match the delivery contract", 2
        )
    if pr.get("baseRefName") != delivery.get("base_branch"):
        raise OrchestratorError(
            "The reviewed PR base branch does not match the delivery contract", 2
        )
    if str(pr.get("mergeable", "")).upper() != "MERGEABLE":
        raise OrchestratorError(
            "Merge-ready requires GitHub to report the PR as mergeable", 2
        )
    if str(pr.get("reviewDecision", "")).upper() == "CHANGES_REQUESTED":
        raise OrchestratorError(
            "Merge-ready cannot be recorded while GitHub has requested changes", 2
        )
    if pr.get("headRefOid") != current_git.get("head"):
        raise OrchestratorError(
            "The PR head commit does not match the commit captured in the Lead review. "
            "Refresh the worktree and review snapshot before marking merge-ready.",
            2,
        )
    gate = github_evidence.get("ci_gate", {})
    gate_state = gate.get("state")
    required_configured = gate.get("required_checks_configured") is True
    if gate_state != "passed" and not gate_exception:
        raise OrchestratorError(
            f"Merge-ready requires passing CI; current gate state is {gate_state!r}",
            2,
        )
    if not required_configured and not gate_exception:
        raise OrchestratorError(
            "No required CI checks were confirmed. Configure required checks or provide "
            "--gate-exception with the project-authorized reason.",
            2,
        )


def command_checkpoint(args: argparse.Namespace) -> int:
    state_root = Path(args.state_root).expanduser().resolve()
    directory, metadata, status = load_session(state_root, args.session)
    delivery = metadata.get("delivery", {"mode": "local"})
    if delivery.get("mode") != "pr":
        raise OrchestratorError(
            "Lead review checkpoints are available only for PR delivery sessions", 2
        )
    state = inferred_state(status)
    if state not in TERMINAL_STATES and args.decision != "blocked":
        raise OrchestratorError(
            "Wait for the implementation session to become terminal before recording this decision",
            2,
        )
    cwd = Path(metadata["cwd"])
    summary = checkpoint_summary(args, cwd)
    github_path = directory / "github-review.json"
    github_evidence = read_json(github_path) if github_path.exists() else {}
    git_review_path = directory / "git-review.json"
    reviewed_git = read_json(git_review_path) if git_review_path.exists() else {}
    current_git = git_snapshot(cwd, delivery.get("base_commit"))
    git_checkpoint_path = directory / "git-checkpoint.json"
    atomic_write_json(git_checkpoint_path, current_git)
    decision = args.decision.replace("-", "_")
    if decision == "merge_ready":
        if not (directory / "review.md").exists() or not git_review_path.exists():
            raise OrchestratorError(
                "Run review before recording a merge-ready checkpoint", 2
            )
        validate_merge_ready_checkpoint(
            state,
            delivery,
            reviewed_git,
            current_git,
            github_evidence,
            args.gate_exception,
        )
    record = {
        "schema_version": 1,
        "captured_at": utc_now(),
        "session_id": metadata["session_id"],
        "decision": decision,
        "summary": summary,
        "gate_exception": args.gate_exception,
        "implementation_state": state,
        "delivery": delivery,
        "evidence": {
            "review": str(directory / "review.md"),
            "git_review": str(git_review_path),
            "git_checkpoint": str(git_checkpoint_path),
            "github_review": str(github_path),
            "reviewed_head": reviewed_git.get("head"),
            "checkpoint_head": current_git.get("head"),
            "worktree_status": current_git.get("status_short"),
            "ci_gate": github_evidence.get("ci_gate"),
            "pr": github_evidence.get("pr"),
        },
        "boundary": "This checkpoint records Lead Agent review only; it does not merge, deploy, accept, or release.",
    }
    history_path = directory / "lead-review-history.json"
    history: list[Any] = []
    if history_path.exists():
        try:
            loaded = json.loads(history_path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                history = loaded
        except (OSError, json.JSONDecodeError):
            history = []
    history.append(record)
    atomic_write_json(history_path, history)
    atomic_write_json(directory / "lead-review.json", record)
    update_status(
        directory,
        workflow_state=decision,
        lead_review_decision=decision,
        lead_reviewed_at=record["captured_at"],
        lead_review_stale=False,
    )
    payload = {
        "session_id": metadata["session_id"],
        "decision": decision,
        "lead_review": str(directory / "lead-review.json"),
        "history": str(history_path),
        "boundary": record["boundary"],
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"Lead review checkpoint: {decision}")
        print(f"Record: {directory / 'lead-review.json'}")
        print("No merge, deployment, acceptance, or release action was performed.")
    return 0


def command_logs(args: argparse.Namespace) -> int:
    state_root = Path(args.state_root).expanduser().resolve()
    directory, _, _ = load_session(state_root, args.session)
    path = directory / ("runner.log" if args.runner else "execution.log")
    if not path.exists():
        raise OrchestratorError(f"Log does not exist yet: {path}")
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    print("\n".join(lines[-args.tail :]))
    return 0


def command_stop(args: argparse.Namespace) -> int:
    state_root = Path(args.state_root).expanduser().resolve()
    directory, _, status = load_session(state_root, args.session)
    state = inferred_state(status)
    if state in TERMINAL_STATES:
        print(f"Session is already terminal: {state}")
        return 0
    atomic_write_text(directory / "stop-requested", utc_now() + "\n")
    update_status(directory, state="stop_requested", stop_requested_at=utc_now())
    child_pid = status.get("child_pid")
    worker_pid = status.get("worker_pid")
    target = child_pid if pid_alive(child_pid) else worker_pid
    if not target:
        raise OrchestratorError(
            "No live process was found; the session may be stale", 4
        )
    try:
        if target == child_pid:
            os.killpg(int(target), signal.SIGTERM)
        else:
            os.kill(int(target), signal.SIGTERM)
    except ProcessLookupError:
        raise OrchestratorError(
            "The process exited before the stop request was delivered", 4
        )
    print(
        f"Stop requested for session {args.session}. Check status once after the process exits."
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state-root",
        default=str(default_state_root()),
        help="durable session root (default: %(default)s)",
    )
    parser.add_argument(
        "--registry",
        help="optional JSON profile registry merged after built-ins and user overrides",
    )
    sub = parser.add_subparsers(
        dest="command_name",
        required=True,
        metavar="{scan,start,status,list,review,checkpoint,logs,stop}",
    )

    scan = sub.add_parser(
        "scan", help="detect supported local code CLIs without installing anything"
    )
    scan.add_argument("--cli", help="check one profile or alias")
    scan.add_argument("--json", action="store_true")
    scan.set_defaults(func=command_scan)

    start = sub.add_parser(
        "start", help="start a durable CLI session; background is the default"
    )
    start.add_argument("--cli", required=True)
    start.add_argument("--cwd", required=True)
    start.add_argument("--prompt", required=True)
    start.add_argument("--session")
    start.add_argument(
        "--delivery-mode",
        choices=["local", "pr"],
        default="local",
        help="use pr for AI-native Delivery Issue work that must stop before merge",
    )
    start.add_argument("--issue", help="Delivery Issue number or URL for PR mode")
    start.add_argument("--base-branch", help="target default/base branch for PR mode")
    start.add_argument("--branch", help="exact feature branch for PR mode")
    start.add_argument(
        "--change-type",
        action="append",
        choices=["add", "modify", "delete"],
        help="declared Delivery Issue change type; repeat when needed",
    )
    start.add_argument(
        "--module",
        action="append",
        help="declared affected module; repeat when needed",
    )
    start.add_argument(
        "--pr-url", help="existing PR URL when resuming an open delivery"
    )
    start.add_argument(
        "--allow-main-worktree",
        action="store_true",
        help="allow PR mode in the main worktree only for a project-profile exception",
    )
    start.add_argument("--model")
    start.add_argument("--effort")
    start.add_argument("--extra", help="CLI-specific arguments parsed without a shell")
    start.add_argument(
        "--extra-arg",
        action="append",
        help="one literal CLI-specific argument; repeat as needed",
    )
    start.add_argument(
        "--mode", choices=["background", "foreground", "tmux"], default="background"
    )
    start.add_argument(
        "--timeout",
        type=int,
        default=7200,
        help="seconds before termination; 0 disables (default: 7200)",
    )
    start.add_argument("--allow-provisional", action="store_true")
    start.add_argument(
        "--allow-concurrent",
        action="store_true",
        help="allow another active session in the same Git worktree",
    )
    start.add_argument("--json", action="store_true")
    start.set_defaults(func=command_start)

    status = sub.add_parser("status", help="read one durable session status")
    status.add_argument("--session", required=True)
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=command_status)

    listing = sub.add_parser("list", help="list recent durable sessions")
    listing.add_argument("--cwd")
    listing.add_argument("--limit", type=int, default=20)
    listing.add_argument("--json", action="store_true")
    listing.set_defaults(func=command_list)

    review = sub.add_parser(
        "review",
        help="create a review bundle from status, result, logs, and Git snapshots",
    )
    review.add_argument("--session", required=True)
    review.add_argument(
        "--skip-github",
        action="store_true",
        help="skip the default one-shot GitHub/CI snapshot in PR mode",
    )
    review.add_argument("--json", action="store_true")
    review.set_defaults(func=command_review)

    checkpoint = sub.add_parser(
        "checkpoint",
        help="persist the Lead Agent review decision without merging",
    )
    checkpoint.add_argument("--session", required=True)
    checkpoint.add_argument(
        "--decision",
        required=True,
        choices=["changes-requested", "merge-ready", "blocked"],
    )
    checkpoint_summary_group = checkpoint.add_mutually_exclusive_group(required=True)
    checkpoint_summary_group.add_argument("--summary")
    checkpoint_summary_group.add_argument("--summary-file")
    checkpoint.add_argument(
        "--gate-exception",
        help="explicit project-authorized reason for missing or non-passing CI evidence",
    )
    checkpoint.add_argument("--json", action="store_true")
    checkpoint.set_defaults(func=command_checkpoint)

    logs = sub.add_parser(
        "logs", help="read a bounded log tail only when explicitly needed"
    )
    logs.add_argument("--session", required=True)
    logs.add_argument("--tail", type=int, default=120)
    logs.add_argument("--runner", action="store_true")
    logs.set_defaults(func=command_logs)

    stop = sub.add_parser("stop", help="request termination of one exact session")
    stop.add_argument("--session", required=True)
    stop.set_defaults(func=command_stop)

    worker = sub.add_parser("_worker")
    worker.add_argument("--session", required=True)
    worker.add_argument("--tee", action="store_true")
    worker.set_defaults(func=None)
    return parser


def main() -> int:
    os.umask(0o077)
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command_name == "_worker":
            root = Path(args.state_root).expanduser().resolve()
            return run_worker(session_dir(root, args.session), tee=args.tee)
        return int(args.func(args))
    except OrchestratorError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
