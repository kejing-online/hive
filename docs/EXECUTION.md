# Work-package execution

`execute` connects an installed `task.workplan` to an adapter (Grok, Codex, command, or an external editor). A package `succeeded` means that run met the structured report and write-scope checks. It is not a test result and not a release.

```
pending → starting → running → succeeded | failed
                ↘ uncertain ↗  (reconcile durable results; do not auto-relaunch)
```

Claim and dispatch share one task lock. The supervisor writes a one-shot launch intent, then starts the tool. Unknown start states are not retried automatically.

```bash
python -m hive.cli execute run "$TASK_ID" "$PACKAGE_ID" \
  --repo "$REPO" --owner worker --adapter grok
python -m hive.cli execute status "$TASK_ID"
python -m hive.cli execute reconcile "$TASK_ID" "$DISPATCH_ID"
python -m hive.cli execute drive "$TASK_ID" \
  --repo "$REPO" --owner swarm --capacity 4 --max-packages 16 --max-seconds 600
python -m hive.cli execute cancel "$TASK_ID" "$DISPATCH_ID" \
  --owner swarm --reason "stop and keep artifacts"
```

`swarm` is the queen’s drive: it installs workers and soldiers, then calls this same execute path. Cancel on an external editor records the request; it does not claim to have killed a remote process.

Cross-tool notes: [PORTABLE.md](PORTABLE.md).
