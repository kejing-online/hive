import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hive import execution_adapters
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

    def test_grok_probe_shape_requires_real_response_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "events.jsonl"
            path.write_text(json.dumps({"sessionId": "actual-session", "stopReason": "end_turn", "usage": {"output_tokens": 2}, "structuredOutput": {"status": "completed", "summary": "done", "remaining": []}}, indent=2))
            events = execution_adapters.parse_grok_events(path)
            self.assertEqual(events["session_id"], "actual-session")
            self.assertTrue(events["completed"])
            self.assertEqual(execution_adapters.report_from_output("grok", path)["status"], "completed")
            path.write_text(json.dumps({"sessionId": "requested", "stopReason": "end_turn"}))
            self.assertFalse(execution_adapters.parse_grok_events(path)["completed"])

    def test_grok_failure_is_not_completion(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "events.jsonl"
            path.write_text(json.dumps({"sessionId": "actual", "stopReason": "max_turns", "structuredOutput": {"status": "completed", "summary": "no", "remaining": []}}))
            events = execution_adapters.parse_grok_events(path)
            self.assertFalse(events["completed"])
            self.assertFalse(events["failed"])


if __name__ == "__main__":
    unittest.main()
