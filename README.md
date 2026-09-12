# Hive

Hive is a **local workflow runtime for coding agents**.

It keeps one durable task identity, a fixed stage graph, file leases, and isolated Git worktrees. Models execute work packages. They do not choose the next stage, approve their own work, or deploy.

Hive is not a chatbot, not a tenant SaaS, and not a demo that only implements `add()`.

## Why

A single “do everything” agent mixes planning, coding, review, and release in one transcript. That is hard to audit and easy to bypass.

Hive splits the job:

| Role | Job |
|------|-----|
| Coordinator | Create the task, install a work plan, dispatch packages, integrate |
| Worker | Write only the paths it was given, in an isolated worktree |
| Reviewer / verifier | Bind evidence against captured source; cannot be the implementer |

The stage table is a **workflow** (A → B → C). It is not an open-ended agent that invents the next step.

## Requirements

- Python 3.11+
- Git
- Linux (file locks use POSIX `fcntl`)

Optional executors: Grok CLI, Codex CLI, or any command that speaks the JSONL protocol below.

## Install

```bash
git clone https://github.com/kejing-online/hive.git
cd hive
python -m pip install -e .
```

Check the probe (JSON; `tenant_runtime` is always false):

```bash
hive doctor
# same as:
python -m hive.cli doctor
```

Run tests:

```bash
python -m unittest discover -s tests -q
```

## Concepts

**Task.** Created by `intake`. Ids look like `HIVE-…`. Kind is `development` or `analysis`. Size is `S`, `M`, or `L`.

**State.** JSON files under `HIVE_STATE_DIR` (default `~/.hive`). State is outside worktrees. Do not commit it.

**Nodes (development).**

```
intake → plan → implement → verify → review → integrate → release
```

- `M` / `L` insert `research` after `plan`.
- `L` inserts `security` beside `review`.
- Analysis tasks stop after plan/research/review. They cannot register a delivery.

**Work plan.** Packages with `read_paths`, `write_paths`, dependencies, and acceptance notes. Acceptance strings are **never executed** as commands.

**Execute.** A package runs in a detached Git worktree. Hive records argv, PID, structured events, and a JSON report. Exit code 0 alone is not success.

**Delivery.** After verify/review, `register` freezes the git objects you tested. `integrated` / `released` only accept that SHA. Hive never deploys.

## Swarm

One command to intake, install a plan, and drive workers. It does **not** mark the task released, and it does **not** invent new DAG stages.

```bash
python -m hive.cli swarm "fix the flaky test" \
  --repo /path/to/git \
  --adapter command \
  --command-file /abs/path/argv.json
```

`--adapter` is required unless `HIVE_EXECUTOR_ADAPTER` is set. There is no silent default executor.

With no `--plan`, Hive installs **exactly one** package (`worker-1`) allowed to write `.hive-swarm-output`. Pass `--plan plan.json` for several packages. Packages with overlapping write paths never run at the same time; disjoint writers may run in parallel up to `--capacity`.

Watch the hive:

```bash
python -m hive.cli roster HIVE-…
```

A failed package is not auto-expanded into new packages. A time window ending returns `window_elapsed`, not a release.

## Quick start

```bash
export HIVE_STATE_DIR=$PWD/.hive-state

python -m hive.cli intake "ship the feature" --size S --source-key demo-1
# prints {"ok": true, "task": {"id": "HIVE-…", "nodes": [...]}}

python -m hive.cli status HIVE-…
python -m hive.cli inspect HIVE-…    # human-readable “why it stopped”
python -m hive.cli execute adapters
```

Advance a node only with a real owner and artifact:

```bash
python -m hive.cli node HIVE-… intake succeeded --owner coordinator --artifact notes.md
python -m hive.cli guard HIVE-… --phase integrate
```

Retry a failed node with an explicit reason (no silent loops):

```bash
python -m hive.cli retry HIVE-… implement --owner coordinator --reason "typed test failed"
```

## Work plans

Install a JSON plan after `plan` has succeeded:

```bash
python -m hive.cli workplan install HIVE-… plan.json --owner coordinator
python -m hive.cli workplan status HIVE-…
python -m hive.cli workplan claim HIVE-… pkg-a --owner worker-a
```

`plan.json` shape:

```json
{
  "max_parallel": 2,
  "packages": [
    {
      "id": "pkg-a",
      "title": "API",
      "depends_on": [],
      "read_paths": ["hive/state.py"],
      "write_paths": ["hive/cli.py"],
      "acceptance": ["python -m unittest tests.test_state"],
      "priority": 1,
      "max_attempts": 2
    }
  ]
}
```

Workers claim, heartbeat, then `finish` with an artifact path or `fail` with a reason. `recover` / `retry` need a reason. Hive does not auto-retry.

## Executors

List adapters:

```bash
python -m hive.cli execute adapters
```

Built-in adapters: `grok`, `codex`, `command`, `external`.

**Run a package** (starts a supervised child in a worktree):

```bash
python -m hive.cli execute run HIVE-… pkg-a \
  --repo /path/to/git --owner worker --adapter grok
```

**Drive** a bounded window (does not retry failures, is not a daemon):

```bash
python -m hive.cli execute drive HIVE-… \
  --repo /path/to/git --owner coordinator --capacity 2 \
  --max-packages 3 --max-seconds 600
```

**External editor** (no nested agent process):

```bash
python -m hive.cli execute handoff HIVE-… pkg-a \
  --repo /path/to/git --owner editor --executor my-editor
python -m hive.cli execute attach HIVE-… DISPATCH \
  --owner editor --token TOKEN --session-id SESSION
python -m hive.cli execute heartbeat HIVE-… DISPATCH --owner editor --token TOKEN
python -m hive.cli execute submit HIVE-… DISPATCH --owner editor --token TOKEN --report report.json
```

Submit report fields: `task_id`, `dispatch_id`, integer `attempt`, `session_id`, `status` (`completed` or `blocked`), `summary`, `remaining` (string array). Hive checks the worktree, index, and HEAD. It does not treat a chat “done” as completion.

**Command adapter** — fixed argv JSON, no shell:

```json
["/usr/bin/python3", "/abs/path/worker.py"]
```

```bash
python -m hive.cli execute run HIVE-… pkg-a \
  --repo /path/to/git --owner worker \
  --adapter command --command-file /abs/path/argv.json
```

The child gets `HIVE_WORKTREE`, `HIVE_PROMPT_FILE`, `HIVE_OUTPUT_FILE`, `HIVE_MODEL`. Write `{ "status": "completed", "summary": "…", "remaining": [] }` to `HIVE_OUTPUT_FILE`. On stdout, JSONL:

```json
{"type":"session.started","session_id":"…"}
{"type":"execution.completed","usage":{"output_tokens":1}}
```

Failure: `{"type":"execution.failed"}`. Plain text and process exit 0 are not enough.

Cancel records a request; it does not claim to have killed a remote editor. `reconcile` replays durable results. Hive does not auto-rerun `uncertain` work.

## Evidence and release

Capture the objects you tested, then bind a real report (the report is copied into an immutable receipt):

```bash
python -m hive.cli capture-source HIVE-… /path/to/worktree HEAD --path 'hive/*'
python -m hive.cli bind-evidence HIVE-… verify SNAPSHOT#sha256=… report.md --owner tester
python -m hive.cli register HIVE-… /path/to/repo HEAD
python -m hive.cli guard-ref /path/to/repo HEAD --phase release
```

`released` needs a production **receipt** you already observed. This package never SSHs, never rsyncs, never bumps versions.

## MCP

Stdio server, argv only — not a permission boundary.

```bash
python -m hive.cli mcp --serve
```

Whitelist: `hive_doctor`, `hive_status`, `hive_inspect`.

Not on the whitelist: `ads_server`, `deploy_to_prod`, unrestricted `shell`, `composio`.

## Environment

| Variable | Meaning |
|----------|---------|
| `HIVE_STATE_DIR` | Task JSON directory (default `~/.hive`) |
| `HIVE_DEFAULT_REPO` | Default `owner/name` for factory tickets (empty if unset) |
| `HIVE_TASK_ID` | Injected into worker processes |
| `HIVE_WORKTREE` | Injected: isolated worktree |
| `HIVE_RUNTIME_ROOT` / `HIVE_RUNTIME_REPO` / `HIVE_RUNTIME_BOOTSTRAP` | Required only if you use runtime receipts |

## Layout

```
hive/           Python package
tests/          unittest
docs/hive.md    one-page contract
pyproject.toml
LICENSE         Apache-2.0
```

CLI entry: `hive` → `hive.cli:main`.

## What this repo is not

- A host application or tenant database
- A deploy tool
- An open planner that picks tools and stages by itself
- A copy of any private product git history

Apache License 2.0.
