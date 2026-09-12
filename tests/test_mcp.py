"""MCP whitelist includes classify/start/record and forbids ads_server."""
from __future__ import annotations

import os
import tempfile
import unittest

from hive import mcp_stdio
from hive.classify import classify_goal


class McpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("HIVE_STATE_DIR")
        os.environ["HIVE_STATE_DIR"] = self.tmp.name

    def tearDown(self):
        if self._old is None:
            os.environ.pop("HIVE_STATE_DIR", None)
        else:
            os.environ["HIVE_STATE_DIR"] = self._old
        self.tmp.cleanup()

    def test_whitelist(self):
        listed = mcp_stdio.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {tool["name"] for tool in listed["result"]["tools"]}
        self.assertEqual(names, set(mcp_stdio.WHITELIST))
        self.assertIn("hive_classify", names)
        self.assertIn("hive_start", names)
        self.assertIn("hive_record", names)
        self.assertNotIn("ads_server", names)
        rejected = mcp_stdio.call_tool("ads_server.pause", {})
        self.assertIn("UNKNOWN_TOOL", rejected.get("error") or rejected.get("code") or "")

    def test_classify_and_start_and_record(self):
        sized = classify_goal("hotfix typo in readme")
        self.assertEqual(sized["size"], "S")
        started = mcp_stdio.call_tool("hive_start", {"goal": "ship it", "size": "S", "source_key": "mcp-1"})
        self.assertTrue(started.get("ok") or started.get("task_id"))
        task_id = started["task_id"]
        recorded = mcp_stdio.call_tool("hive_record", {
            "task_id": task_id, "phase": "intake", "verdict": "PASS", "agent": "queen", "notes": "ok",
        })
        self.assertTrue(recorded.get("ok"), recorded)
