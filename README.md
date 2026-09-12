# Hive

Portable multi-worker development OS: one task identity, a fixed stage DAG, file leases, isolated git worktrees, and executor adapters.

Hive is a **workflow scheduler for coding agents**, not a tenant business app and not a chatbot that picks its own next stage.

## Install

```bash
git clone https://github.com/kejing-online/hive.git
cd hive
python -m pip install -e .
hive doctor
# or: python -m hive.cli doctor
```

State lives in `~/.hive` unless you set `HIVE_STATE_DIR`.

## Commands

```bash
python -m hive.cli intake "ship the feature" --size M
python -m hive.cli status HIVE-...
python -m hive.cli execute adapters
```

Workers write git serially. Reviewers are not the authors. Platform write tools (`ads_server`, deploy-to-prod, unrestricted shell) stay off the MCP whitelist.

## Layout

```
hive/          package
tests/         unittest
docs/hive.md   short contract
```

Apache-2.0
