import unittest

from hive.state import HiveError
from hive.workplan import normalize_plan, select_ready, validate_plan


def package(identifier, *, deps=None, reads=None, writes=None, priority=50, attempts=2):
    return {"id": identifier, "title": identifier, "depends_on": deps or [],
            "read_paths": reads or [f"{identifier}/input.py"], "write_paths": writes or [f"{identifier}/output.py"],
            "acceptance": ["targeted test passes"], "priority": priority, "max_attempts": attempts}


class WorkPlanTests(unittest.TestCase):
    def plan(self, *packages, max_parallel=3):
        return normalize_plan({"max_parallel": max_parallel, "packages": list(packages)})

    def test_normalize_isolated_and_dependency_chain(self):
        spec = {"max_parallel": 2, "packages": [package("one"), package("two", deps=["one"])]}
        plan = normalize_plan(spec)
        spec["packages"][0]["id"] = "changed"
        self.assertEqual(plan["packages"][0]["status"], "queued")
        self.assertEqual(select_ready(plan)["ready"], ["one"])
        plan["packages"][0].update(status="succeeded", attempts=1)
        self.assertEqual(select_ready(plan)["ready"], ["two"])

    def test_parallel_disjoint_read_read_and_conflicts(self):
        plan = self.plan(package("a", writes=["src/a.py"]), package("b", writes=["src/b.py"]), package("r1", reads=["docs/x"], writes=["out/one"]), package("r2", reads=["docs/x"], writes=["out/two"]), max_parallel=4)
        self.assertEqual(select_ready(plan)["ready"], ["a", "b", "r1", "r2"])
        conflict = self.plan(package("writer", writes=["src"]), package("reader", reads=["src/a.py"], writes=["out/r"]), package("other", writes=["src/a.py"]))
        picked = select_ready(conflict)
        self.assertEqual(picked["ready"], ["writer"])
        self.assertIn("path conflict", picked["blocked"]["reader"][0])
        self.assertIn("path conflict", picked["blocked"]["other"][0])

    def test_single_side_paths_priority_and_zero_capacity(self):
        plan = self.plan(package("low", reads=[], writes=["a"], priority=1), package("first", reads=["b"], writes=[], priority=9), package("second", reads=["c"], writes=[], priority=9), max_parallel=3)
        self.assertEqual(select_ready(plan)["ready"], ["first", "second", "low"])
        self.assertEqual(select_ready(plan, capacity=0)["ready"], [])
        self.assertEqual(select_ready(plan, capacity=0)["blocked"]["first"], ["capacity exhausted"])

    def test_running_capacity_and_attempt_budget(self):
        plan = self.plan(package("running", writes=["held/file"]), package("next", writes=["free/file"]), package("retry", writes=["retry/file"], attempts=1), max_parallel=2)
        plan["packages"][0].update(status="running", attempts=1, lease={"token": "token", "owner": "worker", "expires_at": "2030-01-01T00:00:00+00:00"})
        plan["packages"][2]["attempts"] = 1
        result = select_ready(plan)
        self.assertEqual(result["active"], ["running"])
        self.assertEqual(result["ready"], ["next"])
        self.assertEqual(result["blocked"]["retry"], ["attempt budget exhausted"])

    def test_rejects_runtime_injection_paths_and_bad_graphs(self):
        bad = package("a")
        bad["status"] = "running"
        with self.assertRaises(HiveError): normalize_plan({"max_parallel": 1, "packages": [bad]})
        for path in ("", "/root/a", "a/../b", "a\\b", "a/*.py"):
            with self.subTest(path=path), self.assertRaises(HiveError):
                normalize_plan({"max_parallel": 1, "packages": [package("a", reads=[path])]})
        with self.assertRaises(HiveError): self.plan(package("a", deps=["missing"]))
        with self.assertRaises(HiveError): self.plan(package("a", deps=["a"]))
        with self.assertRaises(HiveError): self.plan(package("a", deps=["b"]), package("b", deps=["a"]))
        with self.assertRaises(HiveError): normalize_plan({"schema_version": True, "max_parallel": 1, "packages": [package("a")]})
        with self.assertRaises(HiveError): normalize_plan({"max_parallel": 1, "packages": [{**package("a"), "artifact": {}}]})

    def test_rejects_unknown_fields_but_accepts_runtime_receipt_fields(self):
        with self.assertRaises(HiveError): normalize_plan({"max_parallel": 1, "packages": [package("a")], "unknown": True})
        plan = self.plan(package("a"))
        plan["packages"][0].update(status="succeeded", attempts=1, artifact={"path": "/tmp/report", "sha256": "a" * 64})
        validate_plan(plan)
        plan["packages"][0]["unknown"] = "no"
        with self.assertRaises(HiveError): validate_plan(plan)
        plan["packages"][0].pop("unknown")
        plan["packages"][0]["artifact"]["sha256"] = "not-a-digest"
        with self.assertRaises(HiveError): validate_plan(plan)

    def test_large_dependency_chain_is_iterative(self):
        packages = [package(f"p{number}", deps=[] if number == 0 else [f"p{number - 1}"]) for number in range(1500)]
        validate_plan(normalize_plan({"max_parallel": 32, "packages": packages}))


if __name__ == "__main__":
    unittest.main()
