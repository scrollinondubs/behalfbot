---
name: remarkable
description: reMarkable tablet integration - read annotations, send briefings OTA, semantic book search via pgvector.
plugin: behalfbot-remarkable
enabled_when: "chassis.config.yaml modules.remarkable.enabled == true"
---

# reMarkable Plugin

Use this skill when the installer asks about their reMarkable tablet, wants to send a briefing to it, or wants to search their reading notes.

## What this plugin does

- **Browse + read** - list folders, open documents, extract annotations via the reMarkable MCP (cloud mode, no USB required)
- **Book ingestion** - download a book via the tablet's local HTTP API, chunk + embed it into Postgres pgvector for semantic search
- **Briefing push** - send a generated briefing document OTA to the tablet (requires `briefing_push: true` in config)
- **Semantic query** - search across all ingested books using natural-language queries via the `document_chunks` table

## MCP tools available (when enabled)

```
mcp__remarkable__remarkable_browse(path, query)  - browse folders or search by name
mcp__remarkable__remarkable_read(document, ...)  - read document text with pagination
mcp__remarkable__remarkable_recent(limit)        - recently modified documents
mcp__remarkable__remarkable_status()             - check connection status
mcp__remarkable__remarkable_image(document, ...) - get page as PNG with optional OCR
```

## Book ingestion

```bash
python3 plugins/remarkable/scripts/ingest_remarkable_book.py --name "Book Title"
python3 plugins/remarkable/scripts/ingest_remarkable_book.py --id <doc-id>
```

Requires:
- Tablet on local network (USB or WiFi) for the download step
- Ollama running with nomic-embed-text model loaded
- Postgres running with `documents` + `document_chunks` tables (chassis baseline schema)

## Denylist

The plugin enforces a default-allow privacy fence. Edit `plugins/remarkable/config/remarkable_denylist_config.py` to add installer-specific hard-deny folders and suspicious-folder keywords.

Classifications:
- `allow` - proceed with ingest
- `needs_approval` - prompt installer before first read; decision persisted to `remarkable_folder_decisions` table
- `hard_deny` - skip silently, never read

## Auth setup

One-time registration per install:

```bash
# Get one-time code from my.remarkable.com/device/desktop/connect
uvx remarkable-mcp --register <one-time-code>
```

Store the resulting JSON token in Vaultwarden as `Behalf.bot - reMarkable Cloud API token`.
The chassis bootstrap script hydrates `REMARKABLE_TOKEN` from Vaultwarden into `.env`.

## Config

```yaml
# chassis.config.yaml
modules:
  remarkable:
    enabled: true
    cloud_mode: true
    briefing_push: false
    sync_heartbeat: false
```
