"""Standalone Hive package contract: no host-app private paths."""
from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from io import StringIO
from pathlib import Path

from hive.cli import main as hive_main
from hive.doctor import report
from hive.intake import DEFAULT_REPO, ensure_task

ROOT = Path(__file__).resolve().parents[1]
BANNED = re.compile(
    r"kejingmianban|huahuapanda|kejing-prod|kejing-online/kejing|"
    r"/root/kjg-devos|/root/\.kejing-devos|/var/log/kejing|"
    r"customs_ai_profile|kejing-planning|kejing-delivery|kejing-platform|"
    r"KEJING_DEVOS|/usr/local/lib/kejing",
    re.I,
)


class StandaloneContractTests(unittest.TestCase):
    def test_tree_has_no_private_host_markers(self):
        hits = []
        for path in ROOT.rglob("*"):
            if not path.is_file() or ".git" in path.parts or path.suffix not in {".py", ".md", ".toml", ".txt"}:
                continue
            if path.name == "test_standalone.py":
                continue
            text = path.read_text(encoding="utf-8")
            match = BANNED.search(text)
            if match:
                hits.append(f"{path.relative_to(ROOT)}:{match.group(0)}")
        self.assertEqual(hits, [])

    def test_package_layout(self):
        self.assertTrue((ROOT / "hive" / "cli.py").is_file())
        self.assertTrue((ROOT / "pyproject.toml").is_file())
        self.assertFalse((ROOT / "scripts" / "hive").exists())

    def test_doctor(self):
        payload = report()
        self.assertFalse(payload["tenant_runtime"])
        self.assertIn("ads_server", payload["forbidden_tools"])
        self.assertNotIn("ads_server", payload["mcp_tools"])
        self.assertTrue(payload["hive_dag"])
        out = StringIO()
        from contextlib import redirect_stdout
        with redirect_stdout(out):
            self.assertEqual(hive_main(["doctor"]), 0)
        body = json.loads(out.getvalue())
        self.assertFalse(body["task"]["tenant_runtime"])

    def test_intake_without_host_repo(self):
        os.environ.pop("XAI_API_KEY", None)
        self.assertEqual(DEFAULT_REPO, "")
        task = ensure_task("standalone", repo="example/demo", source_key="s1", size="S",
                           state_dir=Path(tempfile.mkdtemp()))
        self.assertTrue(task["id"].startswith("HIVE-"))
