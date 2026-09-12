# Hive

Hive is a **swarm for coding work**: a queen, many workers, and soldiers.

```
Queen (schedules, does not code)
  ├─ Workers  (write only their slice, in isolated git worktrees)
  └─ Soldiers (verify, then review; they are not the authors)
```

You give one goal. The queen splits the repository into worker slices, runs disjoint workers in parallel, then runs soldiers that depend on those workers. Models execute a package. They do not pick the next role, approve their own work, or deploy.

Hive is not a chatbot, not a tenant app, and not a single “do everything” agent.

## Roles

| Role | Who | Does | Must not |
|------|-----|------|----------|
| Queen | `swarm` coordinator | Intake, split work, drive, roster | Implement, review, release |
| Worker | `worker-<slice>` | Write only `write_paths` in a detached worktree | Invent the DAG, review itself, push |
| Soldier | `soldier-verify`, `soldier-review` | Gate workers after they finish | Start before workers, mark `released` |

The queen follows a **fixed stage law** so the swarm cannot wander:

```
intake → plan → implement → verify → review → integrate → release
```

That table is the hive’s constitution, not the product. The product is the bees.

## Install

Python 3.11+, Git, Linux (`fcntl` locks). Optional executors: Grok CLI, Codex CLI, or a command adapter.

```bash
git clone https://github.com/kejing-online/hive.git
cd hive
python -m pip install -e .
hive doctor          # or: python -m hive.cli doctor
python -m unittest discover -s tests -q
```

State lives in `HIVE_STATE_DIR` (default `~/.hive`). Do not commit it.

## Start a swarm

`--adapter` is required (or set `HIVE_EXECUTOR_ADAPTER`). There is no silent default executor.

```bash
python -m hive.cli swarm "fix the flaky tests" \
  --repo /path/to/git \
  --adapter grok
```

Command adapter (fixed argv JSON, no shell):

```bash
python -m hive.cli swarm "fix the flaky tests" \
  --repo /path/to/git \
  --adapter command \
  --command-file /abs/path/argv.json
```

With no `--plan`, the queen scans top-level directories (or files) and builds:

- one **worker** per slice (up to 6)
- **soldier-verify** (depends on every worker)
- **soldier-review** (depends on verify)

Disjoint workers may run together up to `--capacity` (default 4). Overlapping write paths stay serial. A failed package is not exploded into new packages. A time window ending is `window_elapsed`, not a release. `swarm` never sets `released`.

The queen lays **scent** (`need`) on each worker slice. A claiming worker deposits `busy`; on finish it leaves `done` and `unverified`. Soldiers follow `unverified`. Marks decay (half-life 300s). Workers still obey the DAG; soldiers may move on unverified traces without waiting for every edge, but never onto `busy` or `alarm`. See [docs/SCENT.md](docs/SCENT.md).

Pass `--plan plan.json` to supply the graph. Pass `--write path` (repeatable) to choose worker slices yourself.

## Watch the hive

```bash
python -m hive.cli roster HIVE-…
python -m hive.cli scent HIVE-…
```

The JSON names the queen and lists each package’s `role` (`worker` | `soldier`), `status`, and `owner`.

```bash
python -m hive.cli inspect HIVE-…    # why this task stopped
python -m hive.cli status HIVE-…
```

## How a worker runs

Each package gets a detached Git worktree. Hive records argv, PID, structured events, and a JSON report. Process exit 0 is not success.

Child environment: `HIVE_WORKTREE`, `HIVE_PROMPT_FILE`, `HIVE_OUTPUT_FILE`, `HIVE_TASK_ID`, `HIVE_MODEL`.

Write `{ "status": "completed", "summary": "…", "remaining": [] }` to `HIVE_OUTPUT_FILE`. On stdout, JSONL:

```json
{"type":"session.started","session_id":"…"}
{"type":"execution.completed","usage":{"output_tokens":1}}
```

Failure: `{"type":"execution.failed"}`.

Adapters: `grok`, `codex`, `command`, `external`.

```bash
python -m hive.cli execute adapters
python -m hive.cli execute drive HIVE-… --repo /path/to/git --owner swarm --capacity 4
python -m hive.cli execute cancel HIVE-… DISPATCH --owner swarm --reason "stop"
```

`drive` is a bounded window, not a daemon. It does not retry failures. `reconcile` replays durable results; `uncertain` is not auto-relaunched.

External editor (no nested agent): `execute handoff` → `attach` → `heartbeat` → `submit` with `task_id`, `dispatch_id`, `attempt`, `session_id`, `status` (`completed`|`blocked`), `summary`, `remaining`.

## Manual law (when you are not using swarm)

```bash
python -m hive.cli intake "ship the feature" --size M --source-key demo-1
python -m hive.cli node HIVE-… intake succeeded --owner queen --artifact notes.md
python -m hive.cli workplan install HIVE-… plan.json --owner queen
python -m hive.cli retry HIVE-… implement --owner queen --reason "typed test failed"
```

`plan.json` packages need `id`, `title`, `depends_on`, `read_paths`, `write_paths`, `acceptance`, `priority`, `max_attempts`. Acceptance strings are never executed as commands.

Size `M`/`L` insert `research` after `plan`. `L` inserts `security` beside `review`. Analysis tasks cannot register a delivery.

## Evidence and release

Soldiers in a swarm are packages. A **release** is a separate receipt against frozen git objects. This library never SSHs, rsyncs, or bumps versions.

```bash
python -m hive.cli capture-source HIVE-… /path/to/worktree HEAD --path 'hive/*'
python -m hive.cli bind-evidence HIVE-… verify SNAPSHOT#sha256=… report.md --owner soldier
python -m hive.cli register HIVE-… /path/to/repo HEAD
python -m hive.cli guard-ref /path/to/repo HEAD --phase release
```

## Classify, factory, MCP

Size a goal without starting work:

```bash
python -m hive.cli classify "hotfix typo in the readme"
```

Admit a queued factory ticket onto the same Hive identity (pause file at `$HOME/PAUSE` blocks it):

```bash
python -m hive.cli factory admit ticket.json --home "$HIVE_HOME"
python -m hive.cli factory reason ticket.json --home "$HIVE_HOME"
```

Host-specific budget, kernel, and deploy locks stay in the host. This package only does pause, queued-ticket policy, and source-key binding.

MCP whitelist: `hive_classify`, `hive_start`, `hive_record`, `hive_status`, `hive_doctor`, `hive_snapshot`, `hive_inspect`, `hive_roster`, `hive_scent`.

```bash
python -m hive.cli mcp --serve
```

MCP is stdio, argv only — not a permission boundary. Not on the whitelist: `ads_server`, `deploy_to_prod`, unrestricted `shell`, `composio`.

Manual law, adapters, scent, and evidence: [docs/WORKFLOW.md](docs/WORKFLOW.md), [docs/PORTABLE.md](docs/PORTABLE.md), [docs/EXECUTION.md](docs/EXECUTION.md), [docs/SCENT.md](docs/SCENT.md).

## Environment

| Variable | Meaning |
|----------|---------|
| `HIVE_STATE_DIR` | Task JSON directory (default `~/.hive`) |
| `HIVE_EXECUTOR_ADAPTER` | Default adapter when `--adapter` is omitted |
| `HIVE_TASK_ID` | Injected into worker processes |
| `HIVE_WORKTREE` | Injected isolated worktree |
| `HIVE_DEFAULT_REPO` | Optional `owner/name` for factory tickets |
| `HIVE_RUNTIME_*` | Only if you use runtime receipts |

## Layout

```
hive/           Python package (`hive swarm` lives in hive/swarm.py)
tests/
docs/hive.md
pyproject.toml
LICENSE         Apache-2.0
```

CLI: `hive` → `hive.cli:main`.

## What this repo is not

- A queen that also codes and stamps review
- An open planner that invents stages
- A deploy tool or tenant database
- A copy of a private product git history

Apache License 2.0.
