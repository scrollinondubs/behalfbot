# Behalf.bot Autopilot (Chrome extension)

Lets a Behalf.bot installer do one setup task in one tab of the customer's
Chrome while they watch. Plan: `drafts/behalfbot/chrome-autopilot/plan.html`
in new-jaxity. Relay: `AutopilotDO` in `cloudflare/installer/src/autopilot.ts`.

## Install (unpacked)

```
./build.sh                                      # relay = deployed installer
RELAY_ORIGIN=http://localhost:8797 ./build.sh   # relay = local wrangler dev
```

Unzip `dist/behalfbot-autopilot.zip`, open `chrome://extensions`, turn on
Developer mode, click Load unpacked, pick the `behalfbot-autopilot` folder.
Chrome only (Manifest V3, Chrome 116+).

## How a session runs

1. The installer mints a code: `POST /installs/:id/autopilot/code` (admin).
   The code is `XXX-XXX`, works once, and expires after 10 minutes. The
   runbook it names fixes the allowed sites.
2. The customer pastes it in the popup. The extension pairs
   (`POST /autopilot/pair`), binds the current tab if it is on an allowed
   site or opens the runbook's start page, and opens one WebSocket out to
   the relay. Nothing connects in.
3. The driver sends commands through the relay. On every command the
   extension checks the bound tab's URL against the allowlist and the method
   against `ALLOWED_METHODS` (`policy.js`) before it attaches or reads
   anything. Chrome shows its own "started debugging this browser" bar while
   attached.
4. "Your turn" steps detach the debugger and wait for Done in the side panel.
5. `save_secret` reads one field and sends the value only to the relay, which
   holds it for one fetch. The driver gets "captured", never the value.
   Snapshots drop all field values.
6. Disconnect (popup or side panel), Cancel on Chrome's bar, closing the tab,
   or switching the extension off closes the socket; the relay then deletes
   the session token and anything it held.

## Files

| File | Role |
|---|---|
| `manifest.json` | MV3; permissions `debugger`, `tabs`, `storage`, `sidePanel`; host permission for the relay origin only. |
| `config.js` | `RELAY_ORIGIN`. `build.sh` rewrites it and the host permission together. |
| `policy.js` | Pure allowlist and method policy, AX tree compaction. Tested in `test/`. |
| `background.js` | Service worker: pairing, socket, command execution, teardown. |
| `popup.*` | Code entry, status, allowed sites, last session. |
| `sidepanel.*` | Live action log, "Your turn" with Done, Disconnect. |

Tests: `node --test test/` (also run by `npm test` in `cloudflare/installer`).
