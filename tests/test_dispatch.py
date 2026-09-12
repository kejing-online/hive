from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from hive import dispatch
from hive.state import HiveError, create_task, load_task, locked_task, mark_node, _read, _save
from hive.workplan_runtime import install, recover, retry


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.state = Path(self.tmp.name) / "state"
        self.task = create_task("dispatch", size="S", source_key="dispatch", state_dir=self.state)
        for node in ("intake", "plan"): mark_node(self.task["id"], node, "succeeded", owner="p", artifact="x", state_dir=self.state)
        install(self.task["id"], {"max_parallel": 1, "packages": [{"id":"p", "title":"p", "depends_on":[], "read_paths":["hive/state.py"], "write_paths":["hive/dispatch.py"], "acceptance":["unit"], "priority":1, "max_attempts":2}]}, owner="p", state_dir=self.state)
        self.artifact = Path(self.tmp.name) / "artifact"; self.artifact.write_text("ok")
    def tearDown(self): self.tmp.cleanup()
    def test_compete_reserve_ack_settle_and_retry(self):
        def add(owner):
            try: return dispatch.enqueue(self.task["id"], "p", owner=owner, state_dir=self.state)
            except HiveError: return None
        with ThreadPoolExecutor(2) as pool: entries = list(pool.map(add, ("a", "b")))
        entry = next(e for e in entries if e); self.assertEqual(sum(e is not None for e in entries), 1)
        entry = dispatch.reserve(self.task["id"], entry["id"], owner=entry["owner"], token=entry["token"], launch={"repo":"x"}, state_dir=self.state)
        with self.assertRaises(HiveError): dispatch.reserve(self.task["id"], entry["id"], owner=entry["owner"], token=entry["token"], launch={"repo":"x"}, state_dir=self.state)
        entry = dispatch.acknowledge(self.task["id"], entry["id"], owner=entry["owner"], token=entry["token"], session_id="s", state_dir=self.state)
        done = dispatch.settle(self.task["id"], entry["id"], owner=entry["owner"], token=entry["token"], success=False, artifact=str(self.artifact), reason="bad", state_dir=self.state)
        self.assertEqual(done["status"], "failed")
        self.assertEqual(dispatch.settle(self.task["id"], entry["id"], owner=entry["owner"], token=entry["token"], success=False, artifact=str(self.artifact), reason="bad", state_dir=self.state)["status"], "failed")
        with self.assertRaises(HiveError): dispatch.settle(self.task["id"], entry["id"], owner=entry["owner"], token=entry["token"], success=True, artifact=str(self.artifact), state_dir=self.state)
        self.assertEqual(retry(self.task["id"], "p", owner="a", reason="retry", state_dir=self.state)["status"], "queued")
    def test_uncertain_fences_recovery(self):
        entry = dispatch.enqueue(self.task["id"], "p", owner="a", state_dir=self.state)
        dispatch.mark_uncertain(self.task["id"], entry["id"], owner="a", token=entry["token"], reason="unknown", state_dir=self.state)
        with self.assertRaises(HiveError): recover(self.task["id"], owner="a", reason="expired", state_dir=self.state)

    def test_terminal_replay_binds_expired_uncertain_session_once(self):
        entry = dispatch.enqueue(self.task["id"], "p", owner="a", state_dir=self.state)
        entry = dispatch.reserve(self.task["id"], entry["id"], owner="a", token=entry["token"], launch={"repo":"x"}, state_dir=self.state)
        dispatch.mark_uncertain(self.task["id"], entry["id"], owner="a", token=entry["token"], reason="unknown", state_dir=self.state)
        with locked_task(self.task["id"], self.state) as directory:
            task = _read(self.task["id"], directory)
            task["workplan"]["packages"][0]["lease"]["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            _save(task, directory)
        with self.assertRaises(HiveError):
            dispatch.acknowledge(self.task["id"], entry["id"], owner="a", token=entry["token"], session_id="s", state_dir=self.state)
        replayed = dispatch.acknowledge(self.task["id"], entry["id"], owner="a", token=entry["token"], session_id="s", terminal_replay=True, state_dir=self.state)
        self.assertEqual((replayed["status"], replayed["session_id"]), ("running", "s"))
        with self.assertRaises(HiveError):
            dispatch.acknowledge(self.task["id"], entry["id"], owner="a", token=entry["token"], session_id="other", terminal_replay=True, state_dir=self.state)

    def test_terminal_replay_of_settled_session_is_revision_free(self):
        entry = dispatch.enqueue(self.task["id"], "p", owner="a", state_dir=self.state)
        entry = dispatch.reserve(self.task["id"], entry["id"], owner="a", token=entry["token"], launch={"repo":"x"}, state_dir=self.state)
        entry = dispatch.acknowledge(self.task["id"], entry["id"], owner="a", token=entry["token"], session_id="s", state_dir=self.state)
        dispatch.settle(self.task["id"], entry["id"], owner="a", token=entry["token"], success=True, artifact=str(self.artifact), state_dir=self.state)
        revision = load_task(self.task["id"], state_dir=self.state)["revision"]
        replayed = dispatch.acknowledge(self.task["id"], entry["id"], owner="a", token=entry["token"], session_id="s", terminal_replay=True, state_dir=self.state)
        self.assertEqual(replayed["status"], "succeeded")
        self.assertEqual(load_task(self.task["id"], state_dir=self.state)["revision"], revision)
        with self.assertRaises(HiveError):
            dispatch.acknowledge(self.task["id"], entry["id"], owner="a", token=entry["token"], session_id="other", terminal_replay=True, state_dir=self.state)

    def test_owner_token_freeze_and_expiry_are_fenced(self):
        entry = dispatch.enqueue(self.task["id"], "p", owner="a", state_dir=self.state)
        with self.assertRaises(HiveError):
            dispatch.reserve(self.task["id"], entry["id"], owner="other", token=entry["token"], launch={"repo":"x"}, state_dir=self.state)
        with self.assertRaises(HiveError):
            dispatch.reserve(self.task["id"], entry["id"], owner="a", token="wrong", launch={"repo":"x"}, state_dir=self.state)
        with locked_task(self.task["id"], self.state) as directory:
            task = _read(self.task["id"], directory)
            task["workplan"]["packages"][0]["lease"]["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            _save(task, directory)
        with self.assertRaises(HiveError):
            dispatch.reserve(self.task["id"], entry["id"], owner="a", token=entry["token"], launch={"repo":"x"}, state_dir=self.state)
        with locked_task(self.task["id"], self.state) as directory:
            task = _read(self.task["id"], directory); task["frozen"] = True; _save(task, directory)
        with self.assertRaises(HiveError): dispatch.enqueue(self.task["id"], "p", owner="b", state_dir=self.state)

    def test_freeze_blocks_reserve_and_settle(self):
        entry = dispatch.enqueue(self.task["id"], "p", owner="a", state_dir=self.state)
        with locked_task(self.task["id"], self.state) as directory:
            task = _read(self.task["id"], directory); task["frozen"] = True; _save(task, directory)
        with self.assertRaises(HiveError):
            dispatch.reserve(self.task["id"], entry["id"], owner="a", token=entry["token"], launch={"repo":"x"}, state_dir=self.state)
        with self.assertRaises(HiveError):
            dispatch.settle(self.task["id"], entry["id"], owner="a", token=entry["token"], success=False, artifact=str(self.artifact), reason="stop", state_dir=self.state)

    def test_old_attempt_cannot_change_new_package_attempt(self):
        first = dispatch.enqueue(self.task["id"], "p", owner="a", state_dir=self.state)
        dispatch.settle(self.task["id"], first["id"], owner="a", token=first["token"], success=False, artifact=str(self.artifact), reason="bad", state_dir=self.state)
        retry(self.task["id"], "p", owner="a", reason="retry", state_dir=self.state)
        second = dispatch.enqueue(self.task["id"], "p", owner="b", state_dir=self.state)
        with self.assertRaises(HiveError):
            dispatch.settle(self.task["id"], first["id"], owner="a", token=first["token"], success=True, artifact=str(self.artifact), state_dir=self.state)
        package = load_task(self.task["id"], state_dir=self.state)["workplan"]["packages"][0]
        self.assertEqual((package["attempts"], package["lease"]["token"]), (2, second["token"]))

    def test_validator_rejects_bad_identity_shapes(self):
        task = load_task(self.task["id"], state_dir=self.state)
        task["dispatches"] = None
        with self.assertRaises(HiveError): dispatch.validate_dispatches(task)
        task["dispatches"] = {"D-not-valid": {"id": "D-not-valid"}}
        with self.assertRaises(HiveError): dispatch.validate_dispatches(task)
        entry = dispatch.enqueue(self.task["id"], "p", owner="a", state_dir=self.state)
        task = load_task(self.task["id"], state_dir=self.state); task["dispatches"][entry["id"]]["attempt"] = True
        with self.assertRaises(HiveError): dispatch.validate_dispatches(task)

if __name__ == "__main__": unittest.main()
