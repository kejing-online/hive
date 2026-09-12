import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from hive import execution_adapters, isolation
from hive.state import HiveError


class ExecutionAdapterTests(unittest.TestCase):
    def test_command_file_is_json_argv_and_never_shell(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "command.json"
            path.write_text(json.dumps(["fixture", "a; echo unsafe"]))
            with patch("hive.execution_adapters.shutil.which", return_value="/bin/echo"):
                spec = execution_adapters.select("command", command_file=path)
            self.assertEqual(spec["argv"], [str(Path("/bin/echo").resolve()), "a; echo unsafe"])
            path.write_text(json.dumps("not argv"))
            with self.assertRaises(HiveError):
                execution_adapters.select("command", command_file=path)

    def test_command_read_roots_include_script_parent(self):
        with tempfile.TemporaryDirectory() as temp:
            script = Path(temp) / "worker.py"
            script.write_text("print(1)\n")
            roots = {str(path) for path in isolation.command_read_roots([], [sys.executable, str(script)])}
            self.assertIn(str(script.resolve().parent), roots)

    def test_grok_probe_shape_requires_real_response_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "events.jsonl"
            path.write_text(json.dumps({"sessionId": "actual-session", "stopReason": "end_turn", "usage": {"output_tokens": 2}, "structuredOutput": {"status": "completed", "summary": "done", "remaining": []}}, indent=2))
            events = execution_adapters.parse_grok_events(path)
            self.assertEqual(events["session_id"], "actual-session")
            self.assertTrue(events["completed"])
            self.assertEqual(execution_adapters.report_from_output("grok", path)["status"], "completed")
            path.write_text(json.dumps({"sessionId": "plain-turn", "stopReason": "end_turn", "text": "Hi"}))
            events = execution_adapters.parse_grok_events(path)
            self.assertTrue(events["completed"])
            self.assertEqual(execution_adapters.report_from_output("grok", path)["summary"], "Hi")

    def test_grok_failure_is_not_completion(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "events.jsonl"
            path.write_text(json.dumps({"sessionId": "actual", "stopReason": "max_turns", "structuredOutput": {"status": "completed", "summary": "no", "remaining": []}}))
            events = execution_adapters.parse_grok_events(path)
            self.assertFalse(events["completed"])
            self.assertFalse(events["failed"])

    def test_grok_json_schema_is_not_on_argv(self):
        spec = {"name": "grok", "binary": "/bin/true", "stdin_prompt": False}
        command = execution_adapters.build(
            spec, worktree="/tmp", prompt_file=Path("/tmp/p"), output_file=Path("/tmp/o"),
            schema_file=Path("/tmp/p"), model=None,
        )
        self.assertNotIn("--json-schema", command)
        self.assertIn("--disable-web-search", command)


if __name__ == "__main__":
    unittest.main()
