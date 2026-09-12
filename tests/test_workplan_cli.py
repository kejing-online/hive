"""Scoped CLI and existing delivery-gate integration for Hive work packages."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest

from hive.cli import main
from hive.completion import completion
from hive.guard import validate
from hive.state import HiveError, create_task, load_task, mark_node, validate_task


class WorkplanCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.state = self.home / "state"
        self.task_id = create_task("scoped CLI integration", size="S", state_dir=self.state)["id"]
        for node in ("intake", "plan"):
            mark_node(self.task_id, node, "succeeded", owner="planner", artifact="actual CLI test setup", state_dir=self.state)
        self.spec = self.home / "plan.json"
        self.spec.write_text(json.dumps({"schema_version": 1, "max_parallel": 1, "packages": [
            {"id": "code", "title": "implementation", "depends_on": [], "read_paths": [],
             "write_paths": ["scripts/hive/example.py"], "acceptance": ["targeted validation"],
             "priority": 50, "max_attempts": 2}]}))

    def command(self, *args):
        stream = io.StringIO()
        with redirect_stdout(stream):
            rc = main(["workplan", *args, "--state-dir", str(self.state)])
        return rc, json.loads(stream.getvalue())

    def install(self):
        rc, result = self.command("install", self.task_id, str(self.spec), "--owner", "planner")
        self.assertEqual(rc, 0, result)

    def test_real_cli_lifecycle_leaves_verification_and_release_pending(self):
        self.install()
        before = load_task(self.task_id, state_dir=self.state)
        rc, projection = self.command("status", self.task_id, "--capacity", "0")
        self.assertEqual(rc, 0)
        self.assertEqual(projection["task"]["ready"], [])
        self.assertEqual(load_task(self.task_id, state_dir=self.state), before)
        rc, result = self.command("claim", self.task_id, "code", "--owner", "worker")
        self.assertEqual(rc, 0, result)
        token = result["task"]["lease"]["token"]
        rc, result = self.command("heartbeat", self.task_id, "code", "--owner", "worker", "--token", token)
        self.assertEqual(rc, 0, result)
        report = self.home / "result.md"
        report.write_text("Actual integration-test artifact; not production verification.\n")
        rc, result = self.command("finish", self.task_id, "code", "--owner", "worker", "--token", token, "--artifact", str(report))
        self.assertEqual(rc, 0, result)
        mark_node(self.task_id, "implement", "succeeded", owner="worker", artifact=str(report), state_dir=self.state)
        task = load_task(self.task_id, state_dir=self.state)
        self.assertEqual(next(n for n in task["nodes"] if n["id"] == "verify")["status"], "queued")
        with self.assertRaisesRegex(HiveError, "incomplete node: verify"):
            validate(task)
        self.assertFalse(completion(task, directory=self.state)[0])

    def test_unfinished_packages_block_implementation_gate_and_completion(self):
        self.install()
        with self.assertRaisesRegex(HiveError, "completed work packages"):
            mark_node(self.task_id, "implement", "succeeded", owner="worker", artifact="cannot bypass", state_dir=self.state)
        task = load_task(self.task_id, state_dir=self.state)
        with self.assertRaisesRegex(HiveError, "incomplete work packages"):
            validate(task)
        self.assertIn("incomplete work package: code", completion(task, directory=self.state)[1])

    def test_bad_json_leaves_state_unchanged(self):
        self.spec.write_text("{broken")
        before = load_task(self.task_id, state_dir=self.state)
        rc, result = self.command("install", self.task_id, str(self.spec), "--owner", "planner")
        self.assertEqual(rc, 2)
        self.assertFalse(result["ok"])
        self.assertEqual(load_task(self.task_id, state_dir=self.state), before)

    def test_optional_plan_is_validated_without_changing_legacy_tasks(self):
        task = load_task(self.task_id, state_dir=self.state)
        validate_task(task)
        broken = deepcopy(task)
        broken["workplan"] = None
        with self.assertRaises(HiveError):
            validate_task(broken)


if __name__ == "__main__":
    unittest.main()
