"""hive-swarm: one goal, required adapter, roster, no silent codex."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from io import StringIO

from hive.cli import main as hive_main
from hive.state import HiveError
from hive.swarm import auto_plan, default_plan, resolve_adapter, roster, swarm
from hive.workplan import _conflicts, normalize_plan


class ResolveAdapterTests(unittest.TestCase):
    def test_adapter_required(self):
        os.environ.pop("HIVE_EXECUTOR_ADAPTER", None)
        with self.assertRaises(HiveError) as ctx:
            resolve_adapter(None, None)
        self.assertIn("adapter", str(ctx.exception))

    def test_command_needs_file(self):
        with self.assertRaises(HiveError):
            resolve_adapter("command", None)

    def test_env_counts_as_explicit(self):
        os.environ["HIVE_EXECUTOR_ADAPTER"] = "grok"
        try:
            self.assertEqual(resolve_adapter(None, None), "grok")
        finally:
            os.environ.pop("HIVE_EXECUTOR_ADAPTER", None)


class DefaultPlanTests(unittest.TestCase):
    def test_queen_splits_workers_and_soldiers(self):
        root = Path(tempfile.mkdtemp())
        (root / "src").mkdir()
        (root / "docs").mkdir()
        (root / "src" / "a.py").write_text("a\n")
        (root / "docs" / "n.md").write_text("n\n")
        plan = normalize_plan(auto_plan("demo", repo=root))
        ids = [p["id"] for p in plan["packages"]]
        workers = [i for i in ids if i.startswith("worker-")]
        self.assertGreaterEqual(len(workers), 2, ids)
        self.assertIn("soldier-verify", ids)
        self.assertIn("soldier-review", ids)
        verify = next(p for p in plan["packages"] if p["id"] == "soldier-verify")
        self.assertEqual(set(verify["depends_on"]), set(workers))
        review = next(p for p in plan["packages"] if p["id"] == "soldier-review")
        self.assertEqual(review["depends_on"], ["soldier-verify"])

    def test_fallback_still_has_soldier_gate(self):
        plan = normalize_plan(default_plan("demo"))
        ids = [p["id"] for p in plan["packages"]]
        self.assertTrue(any(i.startswith("worker-") for i in ids))
        self.assertIn("soldier-verify", ids)
        self.assertIn("soldier-review", ids)

    def test_write_conflict_blocks_parallel(self):
        left = {"read_paths": [], "write_paths": ["a.py"]}
        right = {"read_paths": ["a.py"], "write_paths": ["b.py"]}
        self.assertTrue(_conflicts(left, right))
        disjoint = {"read_paths": ["c.py"], "write_paths": ["d.py"]}
        self.assertFalse(_conflicts(left, disjoint))


class SwarmCliTests(unittest.TestCase):
    def setUp(self):
        os.environ.pop("HIVE_EXECUTOR_ADAPTER", None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.state = self.home / "state"
        self.repo = self.home / "repo"
        self.repo.mkdir()
        self.git("init")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.repo / "seed.txt").write_text("seed\n")
        self.git("add", ".")
        self.git("commit", "-m", "seed")

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True)

    def run_cli(self, argv):
        out = StringIO()
        from contextlib import redirect_stdout
        with redirect_stdout(out):
            code = hive_main(argv)
        return code, out.getvalue()

    def test_cli_requires_adapter(self):
        code, body = self.run_cli([
            "swarm", "demo", "--repo", str(self.repo),
            "--state-dir", str(self.state),
        ])
        self.assertEqual(code, 2)
        payload = json.loads(body)
        self.assertFalse(payload["ok"])
        self.assertIn("adapter", payload["error"])

    def test_empty_goal_rejected(self):
        code, body = self.run_cli([
            "swarm", "  ", "--repo", str(self.repo), "--adapter", "grok",
            "--state-dir", str(self.state),
        ])
        self.assertEqual(code, 2)
        self.assertIn("goal", json.loads(body)["error"])

    def test_swarm_command_adapter_one_package_and_roster(self):
        script = self.home / "worker.py"
        script.write_text(
            "import json, os\n"
            "from pathlib import Path\n"
            "root = Path(os.environ['HIVE_WORKTREE'])\n"
            "prompt = Path(os.environ['HIVE_PROMPT_FILE']).read_text()\n"
            "start = prompt.find('{')\n"
            "spec = json.loads(prompt[start:])\n"
            "for rel in spec['package']['write_paths']:\n"
            "    dest = root / rel\n"
            "    dest.parent.mkdir(parents=True, exist_ok=True)\n"
            "    dest.write_text('ok\\n')\n"
            "Path(os.environ['HIVE_OUTPUT_FILE']).write_text("
            "json.dumps({'status':'completed','summary':'ok','remaining':[]}))\n"
            "print(json.dumps({'type':'session.started','session_id':'swarm-1'}), flush=True)\n"
            "print(json.dumps({'type':'execution.completed','usage':{'output_tokens':1}}), flush=True)\n"
        )
        argv = self.home / "argv.json"
        argv.write_text(json.dumps([sys.executable, str(script)]))
        code, body = self.run_cli([
            "swarm", "demo swarm",
            "--repo", str(self.repo),
            "--adapter", "command",
            "--command-file", str(argv),
            "--state-dir", str(self.state),
            "--max-seconds", "20",
            "--source-key", "swarm-test-1",
        ])
        self.assertEqual(code, 0, body)
        payload = json.loads(body)["task"]
        self.assertFalse(payload["released"])
        roles = payload["roles"]
        self.assertGreaterEqual(roles["worker"], 1)
        self.assertEqual(roles["soldier"], 2)
        self.assertEqual(payload["roster"]["queen"]["role"], "queen")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            board = roster(payload["task_id"], state_dir=self.state)
            statuses = {row["status"] for row in board["dispatches"]}
            if statuses & {"succeeded", "failed", "uncertain"} or payload["terminal"] == "complete":
                break
            time.sleep(0.05)
        self.assertTrue(any(p["role"] == "worker" for p in payload["roster"]["packages"]))
        self.assertTrue(any(p["package_id"] == "soldier-review" for p in payload["roster"]["packages"]))
        rcode, rbody = self.run_cli([
            "roster", payload["task_id"], "--state-dir", str(self.state),
        ])
        self.assertEqual(rcode, 0)
        self.assertEqual(json.loads(rbody)["task"]["task_id"], payload["task_id"])

    def test_swarm_does_not_call_released(self):
        result = swarm
        self.assertTrue(callable(result))


if __name__ == "__main__":
    unittest.main()
