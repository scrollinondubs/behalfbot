# Memory graph liveness alert

The daily `memory-canary` check could not complete a write-read-verify round trip against this install's memory MCP knowledge graph. Until this is fixed, the assistant is amnesiac: every session starts with no prior context, every memory write is discarded, and nothing about that is visible in normal operation. A read from a broken graph returns a well-formed empty result, which is indistinguishable from "nothing has been saved yet".

This is not hypothetical. The V1 reference install ran in exactly this state from at least 2026-07-24 to 2026-08-09 - sixteen days - because `.mcp.json` baked an absolute host path that does not exist inside the container namespace. Fixed in #142 / #143; this canary exists so the next one is caught in a day.

## What the gather gives you

```json
{"count": 1, "ok": false, "stage": "...", "error": "...", "resolved_path": "...",
 "shape": "...", "entity": "...", "nonce": "...", "server_cmd": "...",
 "customer_home": "...", "ts_utc": "..."}
```

`resolved_path` is where the memory server was configured to keep the graph, resolved in the namespace the check ran in. `shape` says how that path was derived: `cwd-resolved, no env block` (the current template) or `env.MEMORY_FILE_PATH` (legacy, an absolute path baked into `.mcp.json`).

Lead your report with `resolved_path` and `error`. "Memory is broken" is not actionable; "the graph resolves to `/Users/<host-user>/.behalfbot/memory/memory.jsonl`, which does not exist in this container namespace" is.

## Triage by stage

### `stage: resolve`

`.mcp.json` is missing, unparseable, or has no `mcpServers.memory` block. Re-hydrate it from the current template:

```
python3 ${CHASSIS_HOME}/chassis/scripts/hydrate-mcp-json.py \
  --config ${CUSTOMER_HOME}/chassis.config.yaml \
  --template ${CHASSIS_HOME}/chassis/.mcp.json.template \
  --env ${CUSTOMER_HOME}/.env --output ${CUSTOMER_HOME}/.mcp.json
```

### `stage: precheck`

The resolved path does not exist, or is not writable, in the namespace the check ran in.

- **Directory missing.** Almost always a namespace mismatch: an absolute path valid on the host baked into a config the container reads, or the reverse. If `shape` is `env.MEMORY_FILE_PATH`, that is the #142 bug verbatim. The fix is to drop the baked absolute path and move to the cwd-resolved template shape, which resolves correctly in both namespaces. Do not "fix" it by creating the missing directory inside the container - that produces a second, empty graph and hides the split.
- **Not writable.** Ownership or mode on the graph file or its parent. Check the bind-mount's uid mapping before chowning anything - a chown inside the container can change the file the host sees.

### `stage: write`

The server started but the write did not take. Read `error`:

- A non-zero exit or an unanswered `initialize` means the server could not start at all - usually `npx` failing to resolve `@modelcontextprotocol/server-memory` (no network, no cache) or a broken `command` in `.mcp.json`. If the server cannot start for the canary, it cannot start for Claude either.
- "did not report creating" means a prior canary entity survived a delete, so writes are not persisting. Same class as an unwritable or shadowed graph file.

### `stage: read`

The most important one, and the one the old audit could never catch. The write reported success and a **separate** server invocation then failed to read it back.

- **Read came back EMPTY.** The write and the read are not reaching the same file. Check whether `resolved_path` is a device or a shadowed mount point (`/dev/null` behaves exactly this way: writes succeed, reads return nothing), and whether anything else rewrites that file between the two invocations.
- **Nonce MISMATCH.** The read is served from a stale or different graph. Look for two graph files and a config that disagrees with itself, or a sync process overwriting the file.

### `stage: unavailable`

The canary itself could not run - `memory-canary.sh` missing, or `jq` absent. Treat this as seriously as a real failure: a liveness monitor that cannot run is a silent install. Repair the chassis tree or install the dependency, then run the canary by hand to confirm.

## What to do

1. Run the canary by hand **inside the container** and paste the output. A host-side run proves nothing - a host check passed cleanly the entire sixteen days the container was broken.
   ```
   docker exec -w /app/customer behalfbot bash state/chassis-root/scripts/memory-canary.sh --customer-home /app/customer
   ```
2. Post one line to the install's ops channel: the stage, the resolved path, the error, and the fix you propose.
3. Apply the fix if it is config-side and reversible (re-hydrating `.mcp.json`, correcting a path). Anything that touches the graph file's contents or the bind-mount needs the operator's go-ahead first - the graph is the assistant's continuity and there is no second copy.
4. Re-run the canary and confirm it passes before closing the loop. Do not report it fixed on the strength of the edit.

## Important

- **Never delete or truncate the graph to make the check pass.** Losing the graph is the failure this monitor exists to detect, not a remedy for it.
- The reserved entity `health:memory-canary` is monitor-owned. It is rewritten on every run and holds nothing but a note and a nonce. Ignore it in memory recall, and do not treat it as a real memory.
- The same stage two days running means the first fix did not stick. Escalate rather than repeating it.

## Heartbeat registration

Shipped in `chassis/HEARTBEATS.md.template`, so new installs get it by default. Existing installs must add the block to `${CUSTOMER_HOME}/HEARTBEATS.md` by hand - bootstrap copies the template only when no rendered file exists yet.

Containerized install (both the baked and the mounted-clone layout). The paths are relative to `$CUSTOMER_HOME` and go through the boot-time chassis-root symlink, because `${CHASSIS_HOME}/chassis/...` names the chassis tree on no containerized install - see #151 and the "Containerized installs" note in the template's path conventions:

```yaml
schedule: daily 07:00
gather: state/chassis-root/scripts/gather-memory-canary.sh
condition: threshold count > 0
prompt: state/chassis-root/scheduled-tasks/memory-canary-prompt.md
model: sonnet
budget: 1
criticality: critical
```
