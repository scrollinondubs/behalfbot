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
   Done counts only from a real click (`event.isTrusted`) on the side panel's
   button, and only for the turn the panel is showing (a per-turn nonce the
   worker checks in `policy.messageRefusal`). See "Why a page cannot press
   Done" below.
5. `save_secret` reads one field and sends the value only to the relay, which
   holds it for one fetch. The driver gets "captured", never the value.
   Snapshots drop all field values.
6. These ops run whole inside the extension, so the driver cannot skip their
   safeguards. Their CDP calls are not in `ALLOWED_METHODS`:
   - `screenshot`: a JPEG of the bound tab. Before it leaves the worker, every
     text field and textarea, every frame off the allowed sites or in another
     process, every field read by `save_secret`, and any text shaped like a
     credential is painted black on an OffscreenCanvas. The page DOM is not
     touched. If the tab's URL changes during capture, nothing is sent.
   - `clear`: focuses the ref (clicking it if `DOM.focus` does not land),
     presses select-all and Backspace as real key events with the customer's
     modifier (Cmd on a Mac, Ctrl elsewhere, from `chrome.runtime.getPlatformInfo`),
     and reports whether the field is now empty. Works on inputs, textareas,
     contenteditable, Monaco and CodeMirror 5/6. It never returns the text.
   - `challenge`: looks for a bot check (Cloudflare interstitial, reCAPTCHA,
     hCaptcha, Turnstile, DataDome, PerimeterX, Arkose, AWS WAF) in the pierced
     DOM tree, frame boxes and tab title, and answers with a fixed kind and
     whether it blocks. The driver turns a blocking one into a human step.
   - `click`, `type`: the pointer moves to a point near the middle of the
     element and presses; typing goes in one key event per ordinary
     character (newlines, tabs and emoji through `insertText`, so Enter never
     submits a form). `type` can `clear` first and stops if that fails.
   - `select_option`: sets a native `<select>` by option value or label
     through the prototype setter, then fires bubbling `input` and `change`,
     so React-controlled selects (Stripe's coupon Duration) take it. A
     custom dropdown is refused with "not a native select".
7. Pacing. Each `click`, `type`, `select_option` and `Page.navigate` carries
   `pacing`: `human` (the default when absent) waits 150 to 600 ms before the
   action, moves the mouse in a few eased steps to a point a little off
   centre, and types with 30 to 120 ms between keys and the odd longer
   pause. `off` does none of that (tests). This is timing only: no user
   agent or fingerprint changes, and a bot check still goes to the person.
8. A command that fails with "Cannot access a chrome-extension:// URL of
   different extension" (another extension's frame in the way) is retried
   once after 800 ms, and only if nothing reached the page yet. Every
   `chrome.debugger` call has a deadline, so one stuck call cannot wedge the
   queue.
9. A dropped socket does not end the session (#281). The worker pings every
   20 s and calls a Chrome API on the same beat so it is not stopped while
   the debugger is off the tab. No pong for 45 s means the socket is dead:
   the worker reconnects with the same token (backoff up to 15 s, for up to
   100 s) and the relay holds the session for 2 minutes. Replies made while
   the socket was down, such as Done, are sent first on the new socket; a
   Done lost in the dead socket is answered again when the relay re-sends
   that turn. Other commands in flight fail with "reconnected before
   answering" and are never replayed.
10. Disconnect (popup or side panel), Cancel on Chrome's bar or closing the
   tab sends `bye` and closes the socket; the relay then deletes the session
   token and anything it held at once. Switching the extension off cannot
   say goodbye, so the relay ends that session when its 2 minute hold runs
   out.

## Why a page cannot press Done

- The manifest has no content scripts, no `externally_connectable` and no
  `web_accessible_resources`, so a web page has no `chrome.runtime` channel to
  the worker and cannot load the side panel in a frame or tab.
- `chrome.storage.session` is readable by extension pages only (Chrome's
  default access level), so a page cannot read the turn nonce.
- The driver's CDP reaches only the bound tab, the debugger is detached for
  the whole turn, and every command except `note` is refused until Done.
- The worker accepts `done` only from `sidepanel.html` with the nonce of the
  turn on screen; the side panel sends it only on a trusted click.

`test/e2e/run.mjs` checks this in Chrome: a scripted `.click()`, a synthetic
`MouseEvent`, a `done` from the popup and a stale nonce all fail, and a real
input-event click succeeds.

## Files

| File | Role |
|---|---|
| `manifest.json` | MV3; permissions `debugger`, `tabs`, `storage`, `sidePanel`; host permission for the relay origin only. |
| `config.js` | `RELAY_ORIGIN`. `build.sh` rewrites it and the host permission together. |
| `policy.js` | Pure allowlist and method policy, AX tree compaction, screenshot masking targets, clear key sequence, bot-check classifier, message guard. Tested in `test/`. |
| `background.js` | Service worker: pairing, socket and reconnect, command execution, pacing, teardown. |
| `popup.*` | Code entry, status, allowed sites, last session. |
| `sidepanel.*` | Live action log, "Your turn" with Done, Disconnect. |

Tests: `node --test test/` (also run by `npm test` in `cloudflare/installer`).
End to end in Chrome against local fixtures and a stub relay:
`RELAY_ORIGIN=http://127.0.0.1:8797 ./build.sh && node test/e2e/run.mjs`
(header of `run.mjs` has the details).
