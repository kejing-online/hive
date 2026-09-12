"""Focused isolated-worktree checks. Run with unittest."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hive.state import HiveError
from hive.workspaces import assert_checkpoint, checkpoint, inspect, prepare


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        for args in (("init",), ("config", "user.email", "a@b"), ("config", "user.name", "a")):
            self.git(*args)
        (self.repo / "a.txt").write_text("a")
        self.git("add", ".")
        self.git("commit", "-m", "initial")
        self.root = Path(self.tmp.name) / "hive"
        self.ident = hashlib.sha256(b"workspace-test").hexdigest()

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args, cwd=None):
        return subprocess.run(["git", "-C", str(cwd or self.repo), *args], check=True,
                              capture_output=True, text=True).stdout

    def workspace(self):
        return prepare(self.repo, "HEAD", workspace_id=self.ident, root=self.root)

    def test_staged_checkpoint_rejects_different_index_with_same_worktree(self):
        ws = self.workspace()
        tree = Path(ws["worktree"])
        (tree / "a.txt").write_text("B")
        self.git("add", "a.txt", cwd=tree)
        saved = self.root / "checkpoint.json"
        checkpoint(ws, ["a.txt"], remaining=[], output=saved)
        (tree / "a.txt").write_text("C")
        self.git("add", "a.txt", cwd=tree)
        (tree / "a.txt").write_text("B")
        with self.assertRaises(HiveError):
            assert_checkpoint(ws, saved)

    def test_chmod_is_inspected_and_checkpointed(self):
        ws = self.workspace()
        target = Path(ws["worktree"]) / "a.txt"
        os.chmod(target, 0o755)
        result = inspect(ws, ["a.txt"])
        self.assertEqual(result["files"][0]["path"], "a.txt")
        self.assertEqual(result["files"][0]["mode"], 0o755)
        saved = self.root / "mode.json"
        checkpoint(ws, ["a.txt"], remaining=[], output=saved)
        assert_checkpoint(ws, saved)

    def test_new_deleted_and_renamed_special_names_are_all_reported_and_scoped(self):
        (self.repo / "delete me\n.txt").write_text("delete")
        (self.repo / "old name\n.txt").write_text("rename")
        self.git("add", ".")
        self.git("commit", "-m", "special names")
        ws = self.workspace()
        tree = Path(ws["worktree"])
        (tree / "new name\n.txt").write_text("new")
        (tree / "delete me\n.txt").unlink()
        self.git("mv", "old name\n.txt", "renamed name\n.txt", cwd=tree)
        result = inspect(ws, [])
        names = {item["path"] for item in result["files"]}
        expected = {"new name\n.txt", "delete me\n.txt", "old name\n.txt", "renamed name\n.txt"}
        self.assertTrue(expected <= names)
        self.assertTrue(all(f"out of scope: {name}" in result["violations"] for name in expected))

    def test_checkpoint_with_violations_cannot_be_restored(self):
        ws = self.workspace()
        (Path(ws["worktree"]) / "outside.txt").write_text("outside")
        saved = self.root / "bad.json"
        checkpoint(ws, ["a.txt"], remaining=[], output=saved)
        with self.assertRaises(HiveError):
            assert_checkpoint(ws, saved)

    def test_head_commit_is_a_violation_but_checkpoint_is_saved(self):
        ws = self.workspace()
        tree = Path(ws["worktree"])
        (tree / "a.txt").write_text("committed")
        self.git("add", "a.txt", cwd=tree)
        self.git("commit", "-m", "worktree commit", cwd=tree)
        self.assertIn("HEAD differs from base", inspect(ws, ["a.txt"])["violations"])
        saved = self.root / "committed.json"
        checkpoint(ws, ["a.txt"], remaining=[], output=saved)
        self.assertTrue(saved.exists())

    def test_checkpoint_output_rejects_repo_tree_and_parent_traversal(self):
        ws = self.workspace()
        tree = Path(ws["worktree"])
        for output in (self.repo / "checkpoint.json", tree / "checkpoint.json", self.root / "x" / ".." / "checkpoint.json"):
            with self.assertRaises(HiveError):
                checkpoint(ws, ["a.txt"], remaining=[], output=output)

    def test_prepare_rejects_symlinked_root_and_metadata(self):
        actual = Path(self.tmp.name) / "actual"
        actual.mkdir()
        linked = Path(self.tmp.name) / "linked"
        linked.symlink_to(actual, target_is_directory=True)
        with self.assertRaises(HiveError):
            prepare(self.repo, "HEAD", workspace_id=self.ident, root=linked)
        self.root.mkdir()
        (self.root / "metadata").symlink_to(actual, target_is_directory=True)
        with self.assertRaises(HiveError):
            self.workspace()

    def test_metadata_and_checkpoint_leaf_symlinks_are_rejected(self):
        ws = self.workspace()
        metadata = self.root / "metadata" / f"{self.ident}.json"
        external_metadata = Path(self.tmp.name) / "external-manifest.json"
        external_metadata.write_bytes(metadata.read_bytes())
        metadata.unlink()
        metadata.symlink_to(external_metadata)
        with self.assertRaises(HiveError):
            inspect(ws, ["a.txt"])

        checkpoint_path = self.root / "checkpoint.json"
        external_checkpoint = Path(self.tmp.name) / "external-checkpoint.json"
        external_checkpoint.write_text("{}")
        checkpoint_path.symlink_to(external_checkpoint)
        with self.assertRaises(HiveError):
            checkpoint(ws, ["a.txt"], remaining=[], output=checkpoint_path)

    def test_manifest_edit_and_repo_replacement_are_rejected(self):
        ws = self.workspace()
        metadata = self.root / "metadata" / f"{self.ident}.json"
        value = json.loads(metadata.read_text())
        value["base_sha"] = "0" * 40
        metadata.write_text(json.dumps(value))
        with self.assertRaises(HiveError):
            inspect(ws, ["a.txt"])
        metadata.write_text(json.dumps(ws))
        moved = Path(self.tmp.name) / "old-repo"
        self.repo.rename(moved)
        self.repo.mkdir()
        self.git("init")
        (self.repo / "replacement").write_text("replacement")
        self.git("add", ".")
        self.git("commit", "-m", "replacement")
        with self.assertRaises(HiveError):
            inspect(ws, ["a.txt"])

    def test_changed_parent_symlink_does_not_hash_external_file(self):
        (self.repo / "safe").mkdir()
        (self.repo / "safe" / "file.txt").write_text("inside")
        self.git("add", ".")
        self.git("commit", "-m", "safe directory")
        ws = self.workspace()
        tree = Path(ws["worktree"])
        external = Path(self.tmp.name) / "external"
        external.mkdir()
        (external / "file.txt").write_text("must not be read")
        shutil.rmtree(tree / "safe")
        (tree / "safe").symlink_to(external, target_is_directory=True)
        with patch("hive.workspaces._sha") as digest:
            result = inspect(ws, ["safe"])
        digest.assert_not_called()
        self.assertIn("symlink ancestor: safe/file.txt", result["violations"])

    def test_prepare_replay_keeps_original_repo_unmodified(self):
        ws = self.workspace()
        tree = Path(ws["worktree"])
        (tree / "a.txt").write_text("changed only in workspace")
        replay = prepare(self.repo, "HEAD", workspace_id=self.ident, root=self.root)
        self.assertEqual(replay["worktree"], ws["worktree"])
        self.assertEqual((self.repo / "a.txt").read_text(), "a")
        self.assertEqual(self.git("status", "--porcelain"), "")


if __name__ == "__main__":
    unittest.main()
