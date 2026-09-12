# Hive workflow (the law the queen follows)

Hive unifies task identity, the package graph, leases, evidence, and release admission. Agents execute. They do not own another pipeline.

Roles:

- Queen — schedule (`swarm`, `factory admit`, `classify`)
- Workers — write their slice
- Soldiers — verify then review; they are not the authors

Stage law (development):

```
intake → plan → implement → verify → review → integrate → release
```

`M`/`L` insert `research`. `L` inserts `security`. Analysis tasks cannot register a delivery. Implementers cannot approve themselves. Failures keep evidence; retries need an explicit reason.

## Start

```bash
python -m hive.cli classify "concrete goal"
python -m hive.cli intake "concrete goal" --size M --source-key "stable-id"
# or one command:
python -m hive.cli swarm "concrete goal" --repo "$REPO" --adapter grok
```

Reuse the same Hive id. Do not mint a second task for the same source key.

Factory tickets (queued JSON) bind onto that identity without host-specific budget gates:

```bash
python -m hive.cli factory admit ticket.json --home "$HIVE_HOME"
```

## Record through release

```bash
python -m hive.cli capture-source HIVE-ID /abs/worktree HEAD --worktree --path 'hive/*'
python -m hive.cli bind-evidence HIVE-ID verify SNAPSHOT#sha256=DIGEST /abs/report.md --owner SOLDIER
python -m hive.cli register HIVE-ID /abs/worktree HEAD
python -m hive.cli guard-ref /abs/worktree HEAD --phase integrate
```

`bind-evidence` copies the real report into an immutable receipt. Public `node` cannot write integrate/release success; those states come from delivery receipts. This package never deploys.

MCP: `python -m hive.cli mcp --serve` (classify/start/record/status/doctor/snapshot/inspect/roster).
