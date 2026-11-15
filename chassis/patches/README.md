# chassis/patches

Local patches the chassis applies to third-party code it does not own. Each one
is a stopgap with an exit condition. Remove the patch (and its applier call)
once that condition is met.

| Patch | Applied by | Why | Drop it when |
|---|---|---|---|
| `discord-forwarded-messages.patch` | `chassis/scripts/patch-discord-plugin.sh`, called from the rendered `restart-<bot>-discord.sh` before `claude --channels` starts | `discord@claude-plugins-official` 0.0.4 `server.ts` reads only a message's own content and attachments. A forwarded Discord message keeps its text and files in `messageSnapshots`, so forwards reach the session empty and `download_attachment` reports "no attachments". The patch reads the snapshots, flags forwards as `forwarded="true"` in the channel meta (and `forwarded` in `fetch_messages` rows), and tells the model to treat forwarded text as quoted content. | The upstream plugin reads `messageSnapshots` itself. The applier then logs an "upstream may handle forwards now" warning on the new version. |

The applier only patches a `server.ts` whose sha256 is on its known-pristine
list, keeps a `server.ts.orig`, is a no-op on an already patched file, and
skips anything else with a WARN line. To support a new upstream version, check
the patch applies with zero fuzz to that exact file, then add its sha256 to
`PRISTINE_SHAS` in the applier.
