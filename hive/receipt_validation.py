"""Revalidate immutable observer evidence before treating a component as released."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from .evidence import read_artifact
from .state import HiveError


def validate_release_receipt(receipt: dict, component: str, expected_sha: str,
                             directory: Path | None = None) -> dict:
    category = {"development_executor": "runtime", "backend": "production",
                "frontend": "production", "repository": "repository"}.get(component)
    if not category or not isinstance(receipt, dict):
        raise HiveError("invalid component observer receipt")
    if (receipt.get("schema_version") != 1 or receipt.get("observer") != category
            or receipt.get("observed_sha") != expected_sha or receipt.get("component") != component):
        raise HiveError("legacy or mismatched release receipt requires a fresh observer")
    root = directory / "receipts" / category if directory is not None else None
    payload = read_artifact(receipt.get("artifact", ""), root=root)
    if (payload.get("observer") != category or payload.get("schema_version") != 1
            or payload.get("observed_sha") != expected_sha):
        raise HiveError("release artifact observer/SHA mismatch")
    if category == "production":
        from .release_receipts import _receipt
        observation = payload.get("observation")
        _receipt(observation, expected_sha, set(payload.get("components") or []))
        if component not in payload.get("components", []) or not payload.get("remote"):
            raise HiveError("production observer component/remote mismatch")
    elif category == "runtime":
        observation = payload.get("observation") or {}
        if (observation.get("runtime_sha") != expected_sha or observation.get("result") != "complete"
                or observation.get("exit_code") != 0 or observation.get("command") not in
                {"tick", "worker-tick", "team-work", "hive-merge"}):
            raise HiveError("runtime observer execution mismatch")
        try:
            start, finish = (datetime.fromisoformat(observation[key]) for key in ("started_at", "finished_at"))
            if start.tzinfo is None or finish.tzinfo is None or finish < start:
                raise ValueError("invalid interval")
        except (KeyError, TypeError, ValueError) as exc:
            raise HiveError("runtime observer interval mismatch") from exc
        installation = payload.get("installation") or {}
        try:
            status_bytes = bytes.fromhex(installation["status_hex"])
            status = json.loads(status_bytes)
        except (KeyError, TypeError, ValueError) as exc:
            raise HiveError("runtime receipt lacks installed execution evidence") from exc
        if (hashlib.sha256(status_bytes).hexdigest() != installation.get("status_sha256")
                or any(status.get(key) != observation.get(key) for key in
                       ("command", "runtime_sha", "release", "result", "exit_code", "started_at", "finished_at", "bootstrap_sha256", "pid"))
                or str(Path(installation.get("runtime_root", "")) / "releases" / expected_sha) != observation.get("release")
                or not installation.get("canonical_repo") or not installation.get("common")
                or not installation.get("installed_bootstrap")):
            raise HiveError("runtime receipt installed execution evidence mismatch")
        digest = observation.get("bootstrap_sha256", "")
        if (len(digest) != 64 or installation.get("installed_bootstrap_sha256") != digest
                or payload.get("source_bootstrap_sha256") != digest
                or not observation.get("release")):
            raise HiveError("runtime observer bootstrap mismatch")
    elif (payload.get("remote_ref") != "refs/heads/main"
          or payload.get("remote_sha") != expected_sha or not payload.get("repo")):
        raise HiveError("repository observer main mismatch")
    return payload
