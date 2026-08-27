from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "code-cli-session.py"
BUILTIN_REGISTRY = ROOT / "scripts" / "cli-profiles.json"


class RunnerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="local-code-cli-orchestrator-test-"
        )
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.codex_home = self.root / "codex-home"
        self.fake_bin = self.root / "fake-bin"
        self.home.mkdir()
        self.codex_home.mkdir()
        self.fake_bin.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def environment(self, **updates: str) -> dict[str, str]:
        env = dict(os.environ)
        env["HOME"] = str(self.home)
        env["CODEX_HOME"] = str(self.codex_home)
        env["PATH"] = str(self.fake_bin) + os.pathsep + env.get("PATH", "")
        env.update(updates)
        return env

    def run_process(
        self,
        command: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            command,
            cwd=str(cwd or ROOT),
            env=env or self.environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            check=False,
        )
        if check and result.returncode != 0:
            self.fail(
                f"Command failed ({result.returncode}): {' '.join(command)}\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        return result

    def runner(
        self,
        arguments: list[str],
        *,
        env: dict[str, str] | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return self.run_process(
            [sys.executable, str(RUNNER), *arguments],
            env=env,
            check=check,
        )

    def git(self, cwd: Path, *arguments: str) -> str:
        result = self.run_process(["git", "-C", str(cwd), *arguments])
        return result.stdout.strip()

    def executable(self, name: str, content: str) -> Path:
        path = self.fake_bin / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)
        return path

    def registry(self, name: str = "fixture-agent") -> Path:
        path = self.root / "registry.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "profiles": {
                        "fixture": {
                            "label": "Fixture Agent",
                            "aliases": [],
                            "support": "ready",
                            "commands": [name],
                            "command": ["{executable}"],
                            "base_args": [],
                            "prompt": {"transport": "stdin", "args": []},
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        return path

    def wait_for_terminal(self, state_root: Path, session: str) -> dict[str, object]:
        terminal = {"completed", "failed", "timed_out", "cancelled", "stale"}
        deadline = time.monotonic() + 10
        last: dict[str, object] = {}
        while time.monotonic() < deadline:
            result = self.runner(
                [
                    "--state-root",
                    str(state_root),
                    "status",
                    "--session",
                    session,
                    "--json",
                ]
            )
            last = json.loads(result.stdout)
            if last.get("state") in terminal:
                return last
            time.sleep(0.05)
        self.fail(f"Session did not become terminal: {last}")


class RegistryAndSkillTests(RunnerTestCase):
    def test_skill_frontmatter_and_core_profiles(self) -> None:
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(skill.startswith("---\n"))
        self.assertIn("name: local-code-cli-orchestrator", skill)
        registry = json.loads(BUILTIN_REGISTRY.read_text(encoding="utf-8"))
        profiles = registry["profiles"]
        expected = {"codex", "claude", "grok", "droid", "kimi", "pi", "opencode"}
        self.assertTrue(expected.issubset(profiles))
        for profile_id in expected:
            self.assertIn(profiles[profile_id]["support"], {"ready", "provisional"})

    def test_missing_cli_is_reported_without_installation(self) -> None:
        registry = self.registry("definitely-not-installed-code-cli-fixture")
        state_root = self.root / "sessions"
        result = self.runner(
            [
                "--state-root",
                str(state_root),
                "--registry",
                str(registry),
                "scan",
                "--cli",
                "fixture",
            ],
            check=False,
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("No installation was attempted", result.stdout)
        self.assertFalse(state_root.exists())


class DurableSessionTests(RunnerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.executable(
            "fixture-agent",
            "#!/bin/sh\ncat >/dev/null\nprintf 'fixture completed\\n'\n",
        )
        self.registry_path = self.registry()

    def test_background_session_records_durable_artifacts(self) -> None:
        work = self.root / "work"
        work.mkdir()
        prompt = self.root / "task.md"
        prompt.write_text("Make the harmless fixture change.", encoding="utf-8")
        state_root = self.root / "sessions"
        result = self.runner(
            [
                "--state-root",
                str(state_root),
                "--registry",
                str(self.registry_path),
                "start",
                "--cli",
                "fixture",
                "--cwd",
                str(work),
                "--prompt",
                str(prompt),
                "--session",
                "background-fixture",
                "--json",
            ]
        )
        response = json.loads(result.stdout)
        self.assertEqual(response["mode"], "background")
        status = self.wait_for_terminal(state_root, "background-fixture")
        self.assertEqual(status["state"], "completed")
        session = state_root / "background-fixture"
        for name in (
            "session.json",
            "status.json",
            "prompt-source.md",
            "prompt.md",
            "execution.log",
            "runner.log",
            "result.md",
            "git-before.json",
            "git-after.json",
        ):
            self.assertTrue((session / name).exists(), name)
        self.assertIn("fixture completed", (session / "execution.log").read_text())
        metadata = json.loads((session / "session.json").read_text())
        self.assertNotIn(
            "Make the harmless fixture change", metadata["command_preview"]
        )

    def test_pr_review_is_one_shot_and_checkpoint_is_gated(self) -> None:
        source = self.root / "source"
        remote = self.root / "remote.git"
        worktree = self.root / "worktree"
        source.mkdir()
        remote.mkdir()
        self.git(remote, "init", "--bare")
        self.git(source, "init", "-b", "main")
        self.git(source, "config", "user.name", "Fixture")
        self.git(source, "config", "user.email", "fixture@example.invalid")
        (source / "README.md").write_text("fixture\n", encoding="utf-8")
        self.git(source, "add", "README.md")
        self.git(source, "commit", "-m", "init fixture")
        self.git(source, "remote", "add", "origin", str(remote))
        self.git(source, "push", "-u", "origin", "main")
        self.git(
            source,
            "worktree",
            "add",
            str(worktree),
            "-b",
            "feat/issue-1-fixture",
            "origin/main",
        )
        initial_head = self.git(source, "rev-parse", "HEAD")
        gh_log = self.root / "gh-invocations.log"
        self.executable(
            "gh",
            """#!/bin/sh
printf '%s\n' "$*" >> "$CODE_CLI_GH_LOG"
if [ "$1" = "pr" ] && [ "$2" = "view" ]; then
  head_oid=$(git rev-parse HEAD)
  printf '{"number":1,"url":"https://example.invalid/pull/1","title":"Fixture PR","state":"OPEN","isDraft":false,"mergeable":"MERGEABLE","mergeStateStatus":"CLEAN","reviewDecision":"APPROVED","headRefName":"feat/issue-1-fixture","headRefOid":"%s","baseRefName":"main","baseRefOid":"0000000","updatedAt":"2026-01-01T00:00:00Z","mergedAt":null}\n' "$head_oid"
  exit 0
fi
if [ "$1" = "pr" ] && [ "$2" = "checks" ]; then
  required=0
  for argument in "$@"; do
    if [ "$argument" = "--required" ]; then required=1; fi
  done
  if [ "$CODE_CLI_TEST_CI" = "pending" ]; then
    printf '[{"name":"test","state":"IN_PROGRESS","bucket":"pending","link":"https://example.invalid/check/1","workflow":"CI"}]\n'
    exit 8
  fi
  if [ "$required" -eq 1 ]; then
    printf '[{"name":"test","state":"SUCCESS","bucket":"pass","link":"https://example.invalid/check/1","workflow":"CI"}]\n'
  else
    printf '[{"name":"lint","state":"SUCCESS","bucket":"pass","link":"https://example.invalid/check/2","workflow":"CI"},{"name":"test","state":"SUCCESS","bucket":"pass","link":"https://example.invalid/check/1","workflow":"CI"}]\n'
  fi
  exit 0
fi
exit 2
""",
        )
        prompt = self.root / "issue-1.md"
        prompt.write_text("Complete the fixture PR task.", encoding="utf-8")
        state_root = self.root / "sessions"
        env_pending = self.environment(
            CODE_CLI_GH_LOG=str(gh_log), CODE_CLI_TEST_CI="pending"
        )
        start = self.runner(
            [
                "--state-root",
                str(state_root),
                "--registry",
                str(self.registry_path),
                "start",
                "--delivery-mode",
                "pr",
                "--issue",
                "#1",
                "--base-branch",
                "main",
                "--branch",
                "feat/issue-1-fixture",
                "--change-type",
                "modify",
                "--module",
                "fixture",
                "--cli",
                "fixture",
                "--cwd",
                str(worktree),
                "--prompt",
                str(prompt),
                "--session",
                "pr-fixture",
                "--mode",
                "foreground",
            ],
            env=env_pending,
        )
        self.assertIn("Session: pr-fixture", start.stdout)
        self.runner(
            [
                "--state-root",
                str(state_root),
                "review",
                "--session",
                "pr-fixture",
            ],
            env=env_pending,
        )
        status = json.loads((state_root / "pr-fixture" / "status.json").read_text())
        self.assertEqual(status["workflow_state"], "awaiting_ci")
        github = json.loads(
            (state_root / "pr-fixture" / "github-review.json").read_text()
        )
        self.assertEqual(github["capture_mode"], "one_shot")
        self.assertFalse(github["watch_or_poll"])
        pending_checkpoint = self.runner(
            [
                "--state-root",
                str(state_root),
                "checkpoint",
                "--session",
                "pr-fixture",
                "--decision",
                "merge-ready",
                "--summary",
                "Review passed but CI is pending.",
            ],
            env=env_pending,
            check=False,
        )
        self.assertEqual(pending_checkpoint.returncode, 2)

        env_passed = self.environment(
            CODE_CLI_GH_LOG=str(gh_log), CODE_CLI_TEST_CI="passed"
        )
        self.runner(
            [
                "--state-root",
                str(state_root),
                "review",
                "--session",
                "pr-fixture",
            ],
            env=env_passed,
        )
        self.runner(
            [
                "--state-root",
                str(state_root),
                "checkpoint",
                "--session",
                "pr-fixture",
                "--decision",
                "merge-ready",
                "--summary",
                "Exact reviewed commit matches the PR head and required CI passed.",
            ],
            env=env_passed,
        )
        lead = json.loads((state_root / "pr-fixture" / "lead-review.json").read_text())
        self.assertEqual(lead["decision"], "merge_ready")
        self.assertEqual(
            lead["evidence"]["reviewed_head"], lead["evidence"]["checkpoint_head"]
        )
        self.assertEqual(self.git(source, "branch", "--show-current"), "main")
        self.assertEqual(self.git(source, "rev-parse", "HEAD"), initial_head)
        self.assertNotIn("--watch", gh_log.read_text(encoding="utf-8"))

        (worktree / "dirty.tmp").write_text("changed after review\n", encoding="utf-8")
        dirty_checkpoint = self.runner(
            [
                "--state-root",
                str(state_root),
                "checkpoint",
                "--session",
                "pr-fixture",
                "--decision",
                "merge-ready",
                "--summary",
                "This must be rejected because the worktree changed.",
            ],
            env=env_passed,
            check=False,
        )
        self.assertEqual(dirty_checkpoint.returncode, 2)
        self.assertIn("clean delivery worktree", dirty_checkpoint.stderr)


if __name__ == "__main__":
    unittest.main()
