from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest


RUNNER = Path(__file__).resolve().parents[1] / "scripts" / "code-cli-session.py"


class WaitCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.state_root = Path(self.temp.name)
        self.session_id = "wait-test"
        self.session_dir = self.state_root / self.session_id
        self.session_dir.mkdir()
        self._write_json(
            self.session_dir / "session.json",
            {
                "session_id": self.session_id,
                "profile_id": "test-cli",
                "cwd": str(self.state_root / "project"),
                "delivery": {"mode": "local"},
            },
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_json(self, path: Path, value: dict[str, object]) -> None:
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value), encoding="utf-8")
        os.replace(temporary, path)

    def _running_status(self) -> dict[str, object]:
        return {
            "state": "running",
            "worker_pid": os.getpid(),
            "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }

    def _wait(self, timeout: float) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(RUNNER),
                "--state-root",
                str(self.state_root),
                "wait",
                "--session",
                self.session_id,
                "--timeout",
                str(timeout),
                "--interval",
                "0.1",
                "--json",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )

    def test_returns_terminal_failure_as_successful_notification(self) -> None:
        self._write_json(
            self.session_dir / "status.json",
            {"state": "failed", "exit_code": 17},
        )

        completed = self._wait(timeout=1)
        payload = json.loads(completed.stdout)

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(payload["state"], "failed")
        self.assertFalse(payload["wait_timed_out"])

    def test_blocks_until_running_session_completes(self) -> None:
        self._write_json(self.session_dir / "status.json", self._running_status())

        def finish() -> None:
            time.sleep(0.3)
            self._write_json(
                self.session_dir / "status.json",
                {"state": "completed", "exit_code": 0},
            )

        writer = threading.Thread(target=finish)
        writer.start()
        completed = self._wait(timeout=1)
        writer.join()
        payload = json.loads(completed.stdout)

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(payload["state"], "completed")
        self.assertGreaterEqual(payload["waited_seconds"], 0.1)

    def test_wait_bound_does_not_stop_running_session(self) -> None:
        self._write_json(self.session_dir / "status.json", self._running_status())

        completed = self._wait(timeout=0.05)
        payload = json.loads(completed.stdout)

        self.assertEqual(completed.returncode, 6)
        self.assertEqual(payload["state"], "running")
        self.assertTrue(payload["wait_timed_out"])
        self.assertEqual(payload["wait_timeout_seconds"], 0.05)


if __name__ == "__main__":
    unittest.main()
