# Scent (pheromone)

Bees do not pass a chat log. They read decaying marks on paths.

| Kind | Who lays it | Who follows it |
|------|-------------|----------------|
| `need` | Queen, when the plan is installed | Workers |
| `busy` | Worker, on claim | Everyone keeps off |
| `done` / `unverified` | Worker, on finish | Soldiers |
| `verified` / `reviewed` | Soldiers | Stop chasing that slice |
| `alarm` | Failure | Keep away |

Intensity halves every 300 seconds (configurable per task). Below 0.05 the mark is gone.

The stage DAG is still law for workers. Soldiers may follow a strong `unverified` trace even if a dependency node has not been ticked, but never onto a `busy` or `alarm` slice.

```bash
python -m hive.cli scent HIVE-…
python -m hive.cli roster HIVE-…   # includes scent
```
