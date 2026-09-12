# Scent (pheromone)

Bees do not pass a chat log. They read decaying marks on paths.

| Kind | Who lays it | Who follows it |
|------|-------------|----------------|
| `need` | Queen, when the plan is installed | Workers |
| `busy` | Worker, on claim and again on each heartbeat renewal | Everyone keeps off |
| `done` / `unverified` | Worker, on success settlement | Soldiers |
| `verified` / `reviewed` | Soldiers | Stop chasing that slice |
| `alarm` | Worker, on failure settlement | Keep away; the slice sorts later until a retry re-lays `need` |

Intensity halves every 300 seconds (configurable per task). Below 0.05 the mark is gone. Expired marks are cleaned up when state is written, so the field does not grow without bound. `busy` is re-laid on every heartbeat renewal, so a slice with a live lease never looks empty.

Traces transfer on both paths: manual `workplan finish/fail` and the outbox `dispatch.settle` that `hive swarm` and `execute drive` actually use. A successful worker settlement evaporates `busy` and leaves `done` + `unverified`; a failed settlement evaporates `busy` and leaves `alarm`.

Neighbourhood: an `unverified` or `alarm` mark on `src/api.py` also lays a weaker copy on `src`, so bees on a sibling slice can smell it. Recruitment: when a worker finishes, it boosts `need` on still-queued sibling worker slices (a waggle analog). It does not mint new packages and does not bypass the DAG.

The stage DAG is law for workers and soldiers alike: a package whose dependencies are unmet cannot be claimed. Traces never bypass a dependency edge. They only rank legal candidates by `attraction` (`need` attracts workers, `unverified` attracts soldiers, `alarm` and `busy` repel) and stay visible in `roster`, `status`, `hive scent` (`marks` plus per-path `map`), and MCP `hive_scent`.

```bash
python -m hive.cli scent HIVE-…
python -m hive.cli roster HIVE-…   # includes scent
```

MCP: call `hive_scent` with `{"task_id": "HIVE-…"}`.
