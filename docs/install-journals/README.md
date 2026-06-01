# Install Journals - Index

Per-installer journals + cross-installer friction inventory.

## Journals

| Installer | Date | Status | Journal |
|---|---|---|---|
| installer-1 (V1 bare-metal) | 2026-05-06 to 05-07 | LIVE | [installer-1-2026-05-06.md](installer-1-2026-05-06.md) |
| installer-2 (V2 containerized) | TBD | Pending | TBD |

---

## Baked-vs-runbook map

Every install friction item is classified as either **IMAGE BAKED** (automated
away in the Docker image - never hits a future installer) or **RUNBOOK STEP**
(still a human action, but now documented in `docs/INSTALL.md`).

This table is the definitive answer to "what did containerization actually fix?"

| # | Friction item | Source | V2 status |
|---|---|---|---|
| F01 | Python 3.11 vs 3.12 on Debian 12 | installer-1 | IMAGE BAKED |
| F02 | ffmpeg / sqlite3 / python3-yaml not on Debian base | installer-1 | IMAGE BAKED |
| F03 | PEP 668 blocked pip install | installer-1 | IMAGE BAKED |
| F04 | Hardcoded macOS Homebrew paths in dispatcher | installer-1 | IMAGE BAKED |
| F05 | bun not installed; channels plugin hard-requires it | installer-1 | IMAGE BAKED |
| F06 | Discord MESSAGE CONTENT INTENT not toggled | installer-1 | RUNBOOK STEP (Discord-side; can't automate) |
| F07 | .mcp.json.template had conflicting `discord` MCP entry | installer-1 | TEMPLATE FIXED |
| F08 | `subscriptionType: max` insufficient to ID whose account | installer-1 | RUNBOOK STEP (`claude auth status` in INSTALL.md Step 6) |
| F09 | `claude --channels` silent exit without PTY | installer-1 | RUNBOOK STEP (`script -q -c` wrapper in INSTALL.md Step 9) |
| F10 | systemd-user PATH missing `~/.local/bin` | installer-1 | IMAGE BAKED (`ENV PATH` set in Dockerfile) |
| F11 | VW port assumption wrong (stated vs actual) | installer-1 | RUNBOOK STEP (probe with curl before proceeding) |
| F12 | Core modules treated as plugins in validator | installer-1 | DOCUMENTED (LESSONS_FROM_V1.md #33; config schema clarified) |

### Score: 7 IMAGE BAKED, 4 RUNBOOK STEP, 1 TEMPLATE FIXED

The 4 remaining runbook steps are all human-in-the-loop by nature:

- **F06** - Discord portal toggle. Requires the installer's Discord account.
- **F08** - `claude auth status` check. Requires the installer present to confirm their email.
- **F09** - PTY wrapper. Documented as the canonical invocation; no code change needed.
- **F11** - VW port probe. Live system check before any automated step.

None of these can be automated away without either elevated Discord API access
or interactive session presence - which is by design.

---

## How to add a new journal entry

1. Create `docs/install-journals/<installer>-<YYYY-MM-DD>.md` following the
   structure of `installer-1-2026-05-06.md`.
2. Add a row to the Journals table above.
3. For any NEW friction items, add rows to the baked-vs-runbook table and
   classify them. If a friction item is IMAGE BAKED, add it to the Dockerfile
   or entrypoint in the same PR. If RUNBOOK STEP, add it to `docs/INSTALL.md`.
4. Update the score tally.
