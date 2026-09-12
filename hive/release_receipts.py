#!/usr/bin/env python3
"""Consume factual production component receipts for a Hive release.

Usage::

  ssh "$HIVE_RECEIPT_HOST" 'cat /var/log/deployment_receipt.json' | \
    python3 -m hive.release_receipts observe --repo . --expected-ref <sha> \
      --remote "$HIVE_RECEIPT_HOST" --component backend

This module never deploys.  It validates a snapshot read from the target host,
then records only the components that the target host says this run deployed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


from .delivery import guard_ref, released, resolve_ref
from .state import HiveError, load_task, state_directory
from .evidence import write_artifact


COMPONENTS = frozenset({"backend", "frontend"})
_SHA_PREFIX = re.compile(r"^[0-9a-f]{9,40}$")
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_ACTOR = re.compile(r"^[A-Za-z0-9@._-]{1,80}$")


def _bypass_expiry(raw: str, *, now: datetime | None = None) -> datetime:
    text = (raw or "").strip()
    if not text:
        raise HiveError("Hive bypass expiry is required")
    current = now or datetime.now(timezone.utc)
    if text.isdigit():
        expires = datetime.fromtimestamp(int(text), tz=timezone.utc)
    else:
        try:
            expires = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HiveError("Hive bypass expiry must be unix seconds or ISO-8601") from exc
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        expires = expires.astimezone(timezone.utc)
    if expires <= current:
        raise HiveError("Hive bypass expiry has passed")
    return expires


def authorize_bypass(expected_sha: str, *, environ: Mapping[str, str] | None = None,
                     now: datetime | None = None) -> dict[str, Any]:
    """Validate an explicit human bypass. Missing fields never skip the gate."""
    env = os.environ if environ is None else environ
    if str(env.get("HIVE_RELEASE_BYPASS", "0")) != "1":
        raise HiveError("Hive release bypass is not enabled")
    if not _FULL_SHA.fullmatch(expected_sha or ""):
        raise HiveError("Hive bypass requires the resolved 40-character SHA")
    actor = str(env.get("KJ_HIVE_BYPASS_ACTOR", "")).strip()
    reason = str(env.get("KJ_HIVE_BYPASS_REASON", "")).strip()
    claimed = str(env.get("KJ_HIVE_BYPASS_SHA", "")).strip().lower()
    if not _ACTOR.fullmatch(actor):
        raise HiveError("Hive bypass actor is missing or invalid")
    if len(reason) < 8 or len(reason) > 240:
        raise HiveError("Hive bypass reason must be 8-240 characters")
    if claimed != expected_sha:
        raise HiveError("Hive bypass SHA does not match the deploy HEAD")
    expires = _bypass_expiry(str(env.get("KJ_HIVE_BYPASS_EXPIRES", "")), now=now)
    return {
        "actor": actor,
        "reason": reason,
        "sha": expected_sha,
        "expires_at": expires.isoformat(),
    }


def _digest_map(value: Any, paths: frozenset[str], *, absent: frozenset[str] = frozenset()) -> bool:
    return (isinstance(value, dict) and set(value) == paths and
            all(value[path] is None if path in absent else
                isinstance(value[path], str) and bool(re.fullmatch(r"[0-9a-f]{64}", value[path]))
                for path in paths))


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise HiveError("duplicate production receipt JSON key: " + key)
        result[key] = value
    return result


def _receipt(value: Any, expected_sha: str, requested: set[str], *, task_id: str = "") -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HiveError("production receipt must be a JSON object")
    for key in ("requested_ref", "deployed_components", "components", "observed_at", "version"):
        if key not in value:
            raise HiveError("production receipt is missing " + key)
    if value["requested_ref"] != expected_sha:
        raise HiveError("production receipt requested_ref differs from guarded SHA")
    deployed = value["deployed_components"]
    if not isinstance(deployed, list) or not deployed or any(not isinstance(x, str) for x in deployed):
        raise HiveError("production receipt has invalid deployed_components")
    names = set(deployed)
    if len(names) != len(deployed) or not names <= COMPONENTS:
        raise HiveError("production receipt has unknown or duplicate deployed component")
    if names != requested:
        raise HiveError("production receipt deployed components differ from this release invocation")
    records = value["components"]
    if not isinstance(records, dict):
        raise HiveError("production receipt components must be an object")
    for name in names:
        item = records.get(name)
        if not isinstance(item, dict) or item.get("verified") is not True:
            raise HiveError("production receipt has unverified " + name + " component")
        if name == "backend" and item.get("kind"):
            raise HiveError("overlay receipts are not supported in portable Hive")
        if name == "backend" and any(key in item for key in ("source_ref", "baseline_ref", "metadata_sha256")):
            raise HiveError("overlay receipts are not supported in portable Hive")
        ref = item.get("ref")
        if not isinstance(ref, str) or not _SHA_PREFIX.fullmatch(ref) or not expected_sha.startswith(ref):
            raise HiveError("production receipt " + name + " ref differs from guarded SHA")
    try:
        datetime.fromisoformat(str(value["observed_at"]).replace("Z", "+00:00"))
    except ValueError as exc:
        raise HiveError("production receipt observed_at is invalid") from exc
    version = value["version"]
    if not isinstance(version, dict) or not version.get("version_tag"):
        raise HiveError("production receipt has no factual version observation")
    return value


def _artifact(directory: Path, remote: str, receipt: dict[str, Any], components: list[str]) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.:@/-]+", remote):
        raise HiveError("invalid receipt remote label")
    return write_artifact(directory, "production", {"schema_version": 1, "observer": "production",
        "observed_sha": receipt["requested_ref"], "remote": remote,
        "components": sorted(components), "observation": receipt})


def guard(repo: Path, ref: str, *, components: list[str] | None = None,
          state_dir: Path | None = None, task_id: str = "") -> dict[str, Any]:
    gated = guard_ref(repo, ref, phase="release", state_dir=state_dir)
    if task_id and task_id not in gated["task_ids"]:
        raise HiveError("requested Hive task is not authorized for this release SHA")
    if components is not None:
        requested = set(components)
        if not requested or requested - COMPONENTS or len(requested) != len(components):
            raise HiveError("release guard has invalid production component list")
        directory = state_directory(state_dir)
        covered: set[str] = set()
        for hive_id in gated["task_ids"]:
            if task_id and hive_id != task_id:
                continue
            delivery = load_task(hive_id, state_dir=directory).get("delivery") or {}
            required = delivery.get("required_components")
            if not isinstance(required, list):
                raise HiveError("Hive delivery has no required component contract")
            covered.update(required)
        missing = requested - covered
        if missing:
            raise HiveError("no guarded Hive task requires production component: " + ",".join(sorted(missing)))
    return gated


def observe(repo: Path, expected_ref: str, receipt: Any, *, remote: str,
            components: list[str], state_dir: Path | None = None, task_id: str = "") -> list[dict[str, Any]]:
    requested = set(components)
    if not requested or requested - COMPONENTS or len(requested) != len(components):
        raise HiveError("release invocation has invalid component list")
    expected = resolve_ref(repo, expected_ref)
    factual = _receipt(receipt, expected, requested, task_id=task_id)
    directory = state_directory(state_dir)
    if str(os.environ.get("HIVE_RELEASE_BYPASS", "0")) == "1":
        attestation = authorize_bypass(expected)
        artifact = _artifact(directory, remote, factual, components)
        return [{"task_id": None, "components": sorted(requested), "bypass": attestation,
                 "artifact": artifact}]
    gated = guard(repo, expected, components=components, state_dir=state_dir, task_id=task_id)
    artifact = _artifact(directory, remote, factual, components)
    results: list[dict[str, Any]] = []
    for hive_id in gated["task_ids"]:
        # Explicit task means exactly one recipient, for ordinary receipts too.
        if task_id and hive_id != task_id:
            continue
        task = load_task(hive_id, state_dir=directory)
        delivery = task.get("delivery") if isinstance(task, dict) else None
        required = delivery.get("required_components") if isinstance(delivery, dict) else None
        if not isinstance(required, list):
            raise HiveError("Hive delivery has no required component contract")
        applicable = sorted(requested.intersection(required))
        if not applicable:
            continue
        result = released(hive_id, repo, expected, expected, artifact=artifact,
                          component=applicable, state_dir=directory)
        results.append({"task_id": hive_id, "components": applicable, "release": result})
    if not results:
        raise HiveError("no guarded Hive task requires the deployed production component")
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("guard", "observe", "authorize-bypass"):
        command = sub.add_parser(name)
        command.add_argument("--repo", required=True)
        command.add_argument("--expected-ref", required=True)
        command.add_argument("--state-dir", default="")
        if name != "authorize-bypass":
            command.add_argument("--task-id", default="")
    observed = sub.choices["observe"]
    observed.add_argument("--remote", required=True)
    observed.add_argument("--component", action="append", required=True)
    sub.choices["guard"].add_argument("--component", action="append", required=True)
    args = parser.parse_args(argv)
    directory = state_directory(Path(args.state_dir) if args.state_dir else None)
    try:
        if args.command == "authorize-bypass":
            result: Any = authorize_bypass(resolve_ref(Path(args.repo), args.expected_ref))
        elif args.command == "guard":
            result = guard(Path(args.repo), args.expected_ref, components=args.component,
                                state_dir=directory, task_id=args.task_id)
        else:
            try:
                incoming = json.load(sys.stdin, object_pairs_hook=_unique_json)
            except json.JSONDecodeError as exc:
                raise HiveError("target deployment receipt is not valid JSON") from exc
            result = observe(Path(args.repo), args.expected_ref, incoming, remote=args.remote,
                             components=args.component, state_dir=directory, task_id=args.task_id)
    except HiveError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
