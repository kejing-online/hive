"""Portable factory: pause, queued ticket, Hive identity. No host kernel."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from io import StringIO

from hive.cli import main as hive_main
from hive.factory import admission_reason, admit
from hive.state import HiveError


class FactoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def ticket(self, **fields):
        base = {"ticket_id": "T-1", "status": "queued", "title": "fix tests", "repo": "example/demo"}
        base.update(fields)
        return base

    def test_pause_blocks_admit(self):
        (self.home / "PAUSE").write_text("1\n")
        self.assertEqual(admission_reason(self.ticket(), self.home), "factory paused")
        with self.assertRaises(HiveError):
            admit(self.ticket(), self.home)

    def test_admit_binds_hive_task(self):
        card = admit(self.ticket(), self.home)
        self.assertTrue(card["hive_task_id"].startswith("HIVE-"))
        self.assertEqual(card["hive"]["node_id"], "workflow")
        self.assertEqual(card["ticket_id"], "T-1")

    def test_cli_admit(self):
        path = self.home / "t.json"
        path.write_text(json.dumps(self.ticket()), encoding="utf-8")
        out = StringIO()
        from contextlib import redirect_stdout
        with redirect_stdout(out):
            code = hive_main(["factory", "admit", str(path), "--home", str(self.home)])
        self.assertEqual(code, 0, out.getvalue())
        body = json.loads(out.getvalue())
        self.assertTrue(body["ok"])
        self.assertTrue(body["task"]["hive_task_id"].startswith("HIVE-"))
