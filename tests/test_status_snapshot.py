"""Status sources retain their age/errors and do not manufacture completion."""
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from hive.status_snapshot import snapshot


class SnapshotTests(unittest.TestCase):
    def test_runtime_selection_uses_installed_current_json_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "runtime").mkdir()
            (home / "runtime/current.json").write_text(json.dumps({
                "sha": "a" * 40, "hive_task_ids": ["HIVE-CURRENT"],
                "checked_at": "2026-09-08T12:00:00Z", "degraded": "using admitted fallback"}))
            runtime = snapshot(home, now=datetime(2026, 9, 8, 12, 1, tzinfo=timezone.utc), log_dir=home)["sources"]["runtime"]
            self.assertEqual(runtime["freshness"], "fresh")
            self.assertEqual(runtime["data"]["sha"], "a" * 40)
            self.assertEqual(runtime["data"]["hive_task_ids"], ["HIVE-CURRENT"])

    def test_stale_factory_does_not_hide_current_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "queue").mkdir()
            (home / "factory_status.json").write_text(json.dumps({"last_tick": "2026-09-07T00:00:00Z", "counts": {"queued": 0}}))
            (home / "queue/one.json").write_text(json.dumps({"status": "queued", "hive_task_id": "HIVE-ONE"}))
            result = snapshot(home, now=datetime(2026, 9, 8, tzinfo=timezone.utc), log_dir=home)
            self.assertEqual(result["sources"]["factory"]["freshness"], "stale")
            self.assertEqual(result["sources"]["queue"]["status_counts"], {"queued": 1})
            self.assertEqual(result["sources"]["queue"]["hive_bound"], 1)

    def test_broken_and_missing_sources_are_not_empty_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "worker_status.json").write_text("broken")
            result = snapshot(home, log_dir=home)
            self.assertEqual(result["sources"]["worker"]["freshness"], "error")
            self.assertEqual(result["sources"]["factory"]["freshness"], "missing")
            self.assertEqual(result["sources"]["queue"]["freshness"], "missing")

    def test_legacy_complete_is_visible_without_verified_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "hive").mkdir()
            path = home / "hive/HIVE-OLD.json"
            path.write_text(json.dumps({"id": "HIVE-OLD", "kind": "development", "status": "complete", "nodes": []}))
            before = path.read_bytes()
            result = snapshot(home, log_dir=home)["sources"]["hive"]
            self.assertEqual(result["raw_status_counts"], {"complete": 1})
            self.assertEqual(result["verified_complete"], 0)
            self.assertEqual(result["unverified_complete"][0]["id"], "HIVE-OLD")
            self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
