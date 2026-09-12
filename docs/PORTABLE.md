# Use the same Hive from Grok or another editor

Hive stores the task, graph, path leases, worktrees, checkpoints, and delivery evidence. Editors do not own a second identity. Switch tools and keep the same `HIVE-…` id.

```bash
python -m hive.cli execute adapters
python -m hive.cli execute run "$TASK_ID" "$PACKAGE_ID" \
  --repo "$REPO" --owner worker --adapter grok
python -m hive.cli execute drive "$TASK_ID" \
  --repo "$REPO" --owner swarm --adapter grok --capacity 4
```

`--adapter` selects the tool. `--model` selects that tool’s model. Set `HIVE_EXECUTOR_ADAPTER=grok` in the shell; an explicit `--adapter` wins. If Grok is missing, fail closed — do not silently start Codex.

## External editor

If an editor already has an agent session, do not nest another one:

```bash
python -m hive.cli execute handoff "$TASK_ID" "$PACKAGE_ID" \
  --repo "$REPO" --owner editor-a --executor my-editor
python -m hive.cli execute attach "$TASK_ID" "$DISPATCH_ID" \
  --owner editor-a --token "$LEASE_TOKEN" --session-id "$SESSION"
python -m hive.cli execute heartbeat "$TASK_ID" "$DISPATCH_ID" \
  --owner editor-a --token "$LEASE_TOKEN"
python -m hive.cli execute submit "$TASK_ID" "$DISPATCH_ID" \
  --owner editor-a --token "$LEASE_TOKEN" --report "$REPORT_JSON"
```

Report fields: `task_id`, `dispatch_id`, integer `attempt`, `session_id`, `status` (`completed`|`blocked`), `summary`, `remaining`.

## Command protocol

Write an argv JSON file such as `["/abs/worker", "--headless"]`. Pass `--adapter command --command-file /abs/argv.json`. Hive uses a fixed argv list, never a shell.

The child receives `HIVE_WORKTREE`, `HIVE_PROMPT_FILE`, `HIVE_OUTPUT_FILE`, `HIVE_MODEL`. Write `{ "status": "completed", "summary": "…", "remaining": [] }` to the output file and JSONL events on stdout:

```json
{"type":"session.started","session_id":"…"}
{"type":"execution.completed","usage":{"output_tokens":1}}
```

Plain text and exit 0 are not completion.

See also [WORKFLOW.md](WORKFLOW.md) and [EXECUTION.md](EXECUTION.md).
