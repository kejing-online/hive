"""Hive consistency/security-contract regressions. Temporary state; no production."""
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hive.state import (HiveError, attach_binding, create_task, load_task,
                                mark_node, retry_node, state_directory)
from hive.guard import validate


def claim_once(args):
    task_id, directory, owner = args
    try:
        mark_node(task_id, "implement", "leased", owner=owner, state_dir=Path(directory))
        return True
    except HiveError:
        return False


class HiveStateTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state_dir = Path(self.temp.name)

    def task(self, size="S", **kw):
        return create_task("Hive contract", size=size, state_dir=self.state_dir, **kw)

    def advance(self, task, node, **kw):
        return mark_node(task["id"], node, "succeeded", owner=node,
                         artifact="evidence://" + node, state_dir=self.state_dir, **kw)

    def ready(self, size="S"):
        task = self.task(size)
        for node in task["nodes"]:
            if node["id"] not in {"integrate", "release"}:
                task = self.advance(task, node["id"])
        return task

    def test_large_task_requires_research_security_and_approved_integration(self):
        task = self.ready("L")
        self.assertTrue(validate(task)["ready"])
        with self.assertRaisesRegex(HiveError, "explicit approval"):
            self.advance(task, "integrate")
        with self.assertRaisesRegex(HiveError, "reserved for a delivery receipt"):
            self.advance(task, "integrate", approved=True)

    def test_missing_duplicate_and_relinked_required_nodes_are_rejected(self):
        task = self.ready("L")
        broken = []
        missing = deepcopy(task)
        missing["nodes"] = [n for n in missing["nodes"] if n["id"] != "verify"]
        broken.append(missing)
        duplicate = deepcopy(task)
        duplicate["nodes"][3] = deepcopy(duplicate["nodes"][4])
        broken.append(duplicate)
        relinked = deepcopy(task)
        relinked["nodes"][-2]["depends_on"] = []
        broken.append(relinked)
        for value in broken:
            with self.subTest(value=value["nodes"]), self.assertRaises(HiveError):
                validate(value)

    def test_skip_cannot_bypass_gates_and_research_waiver_needs_dependency_and_evidence(self):
        task = self.task("L")
        for node in ("intake", "plan", "implement", "verify", "review", "security", "integrate", "release"):
            with self.subTest(node=node), self.assertRaisesRegex(HiveError, "only research"):
                mark_node(task["id"], node, "skipped", owner="root", approved=True,
                          artifact="waiver", state_dir=self.state_dir)
        with self.assertRaisesRegex(HiveError, "dependencies incomplete"):
            mark_node(task["id"], "research", "skipped", owner="root", approved=True,
                      artifact="waiver", state_dir=self.state_dir)
        self.advance(task, "intake")
        self.advance(task, "plan")
        waived = mark_node(task["id"], "research", "skipped", owner="root", approved=True,
                           artifact="documented scope waiver", state_dir=self.state_dir)
        self.assertEqual(waived["nodes"][2]["status"], "skipped")

    def test_concurrent_claim_has_exactly_one_owner_and_owner_cannot_be_omitted(self):
        task = self.task()
        self.advance(task, "intake")
        self.advance(task, "plan")
        with ProcessPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(claim_once, [(task["id"], str(self.state_dir), f"worker-{i}") for i in range(12)]))
        self.assertEqual(sum(results), 1)
        for status in ("running", "succeeded", "failed", "interrupted"):
            with self.subTest(status=status), self.assertRaises(HiveError):
                mark_node(task["id"], "implement", status, state_dir=self.state_dir)

    def test_concurrent_intake_is_idempotent_and_bindings_are_not_lost(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            tasks = list(pool.map(lambda _: self.task(source_key="repo:bug:42"), range(18)))
        self.assertEqual(len({t["id"] for t in tasks}), 1)
        task_id = tasks[0]["id"]
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda i: attach_binding(task_id, system="test", external_id=str(i),
                          node_id="plan", state_dir=self.state_dir), range(18)))
        self.assertEqual(len(load_task(task_id, state_dir=self.state_dir)["bindings"]), 18)
        self.assertEqual(len(list(self.state_dir.glob("*.json"))), 1)
        with self.assertRaisesRegex(HiveError, "different task size"):
            self.task("L", source_key="repo:bug:42")

    def test_failure_or_rework_revokes_downstream_approval_and_complete(self):
        task = self.ready("L")
        # A public node caller cannot manufacture the two delivery successes.
        with self.assertRaisesRegex(HiveError, "reserved for a delivery receipt"):
            self.advance(task, "integrate", approved=True)
        revised = mark_node(task["id"], "verify", "failed", owner="verify",
                            artifact="regression://new-failure", state_dir=self.state_dir)
        self.assertEqual(revised["status"], "blocked")
        by_id = {n["id"]: n for n in revised["nodes"]}
        for name in ("review", "security", "integrate", "release"):
            self.assertEqual(by_id[name]["status"], "queued")
            self.assertEqual(by_id[name]["artifacts"], [])
            self.assertNotIn("approved_at", by_id[name])
        with self.assertRaises(HiveError):
            validate(revised, phase="release")
        with self.assertRaisesRegex(HiveError, "explicit retry"):
            self.advance(task, "verify")
        with self.assertRaisesRegex(HiveError, "explicit retry"):
            mark_node(task["id"], "verify", "blocked", owner="different-owner",
                      artifact="replace failure", state_dir=self.state_dir)
        retry_node(task["id"], "verify", owner="verify", reason="fixed failing case", state_dir=self.state_dir)
        repaired = self.advance(task, "verify")
        self.assertEqual(repaired["status"], "active")
        self.assertGreater(len(repaired["history"]), len(task["history"]))

    def test_changed_implementation_evidence_invalidates_old_verification(self):
        task = self.ready()
        updated = mark_node(task["id"], "implement", "succeeded", owner="implement",
                            artifact="commit://new-head", state_dir=self.state_dir)
        self.assertEqual(next(n for n in updated["nodes"] if n["id"] == "verify")["status"], "queued")
        with self.assertRaises(HiveError):
            validate(updated)

    def test_active_integration_also_requires_gate_and_approval(self):
        task = self.task()
        with self.assertRaises(HiveError):
            mark_node(task["id"], "integrate", "running", owner="release",
                      approved=True, state_dir=self.state_dir)
        ready = self.ready()
        with self.assertRaisesRegex(HiveError, "explicit approval"):
            mark_node(ready["id"], "integrate", "running", owner="release", state_dir=self.state_dir)

    def test_guard_requires_evidence_and_different_reviewer(self):
        task = self.ready()
        no_evidence = deepcopy(task)
        next(n for n in no_evidence["nodes"] if n["id"] == "verify")["artifacts"] = []
        with self.assertRaisesRegex(HiveError, "verify evidence"):
            validate(no_evidence)
        next(n for n in task["nodes"] if n["id"] == "review")["owner"] = " IMPLEMENT "
        with self.assertRaisesRegex(HiveError, "reviewer must differ"):
            validate(task)

    def test_large_security_actor_must_be_independent_from_implementation(self):
        task = self.ready("L")
        next(node for node in task["nodes"] if node["id"] == "security")["owner"] = "implement"
        with self.assertRaisesRegex(HiveError, "security reviewer must differ"):
            validate(task)
        next(node for node in task["nodes"] if node["id"] == "security")["owner"] = "review"
        self.assertTrue(validate(task)["ready"])

    def test_default_directory_is_shared_and_bad_state_is_not_idle(self):
        with patch.dict(os.environ, {"HIVE_STATE_DIR": str(self.state_dir / "shared")}):
            self.assertEqual(state_directory(), self.state_dir / "shared")
        task = self.task()
        path = self.state_dir / (task["id"] + ".json")
        path.write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(HiveError, "unreadable"):
            load_task(task["id"], state_dir=self.state_dir)

    def test_legacy_complete_does_not_invent_release(self):
        task = self.ready()
        task.pop("schema_version")
        task.pop("history")
        task.pop("revision")
        task["nodes"].pop()
        task["status"] = "complete"
        (self.state_dir / (task["id"] + ".json")).write_text(json.dumps(task))
        legacy = load_task(task["id"], state_dir=self.state_dir)
        self.assertEqual(legacy["nodes"][-1]["id"], "release")
        self.assertEqual(legacy["nodes"][-1]["status"], "queued")
        self.assertNotEqual(legacy["status"], "complete")
        with self.assertRaises(HiveError):
            validate(legacy, phase="complete")


if __name__ == "__main__":
    unittest.main()
