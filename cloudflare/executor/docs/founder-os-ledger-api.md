# FounderOS tenant-scoped ledger API (contract)

Status: **contract, v1.** The executor side is built and tested against a mock
(behalfbot#215). The VCL side is built in
[vibecodelisboa#508](https://github.com/scrollinondubs/vibecodelisboa/issues/508),
on top of the Turso ledger adapter from
[vibecodelisboa#506](https://github.com/scrollinondubs/vibecodelisboa/issues/506).

The reference mock that the executor tests run against is
`founder-os/test/mock-vcl.mjs`. It implements every rule below and is the
quickest way to see the contract as code.

## Why this API exists

A FounderOS coach session runs a founder-os skill on the Cloudflare executor.
The session needs to read and write one founder's ledger. It must never be able
to read or write another founder's rows, even if the skill, the founder's own
input, or a prompt injection inside that input asks it to.

So the executor holds no database credential at all. It holds one short-lived
**session token** that VCL minted for one `founder_id`, and it reaches the
ledger only through the HTTP endpoints below. VCL derives the tenant from the
token on every request. Nothing the executor sends can name a different tenant.

## Session lifecycle

```
student clicks "start coach session" in the VCL dashboard
  VCL: resolve founder_id from the student's login session
  VCL: mint session token T  -> { founder_id, session_id, expires_at }
  VCL -> Worker  POST /founder-os/sessions          (Bearer FOUNDER_OS_TRIGGER_TOKEN)
        { founder_id, stage, skill, message?, artifact_refs?, session_token: T }
    Worker -> FounderOSContainer instance named by founder_id
      shim -> VCL  GET  /me                         (Bearer T)  must equal founder_id
      shim -> VCL  GET  /founder, /stage-progress, ... (context prefetch)
      shim <- 202 to the Worker, then runs claude in the background
      shim -> VCL  POST /artifacts, /pains, ...     (writes the skill asked for)
      shim -> VCL  POST /session/result             (reply + write outcomes)
  VCL: revoke T (or let it expire)
```

The Worker answers 400, 403, 404, 409 or 503 synchronously when the session
cannot start (bad input, token/founder mismatch, stage mismatch, unknown skill,
plugin not pinned). Once it answers 202, the outcome arrives only through
`POST /session/result`.

## Base URL and auth

- Base: `https://vibecodelisboa.com/api/founder-os/v1/ledger` (the executor
  reads it from the `FOUNDER_OS_VCL_API_BASE` Worker var). Every path below is
  relative to it.
- Every request carries `Authorization: Bearer <session token>`.
- `Content-Type: application/json` on every request with a body.

## The session token

| Property | Rule |
|---|---|
| Minted by | VCL, when the dashboard starts a session. Never by the executor. |
| Bound to | exactly one `founder_id` and one `session_id` |
| Lifetime | 15 minutes from mint. The executor's own session cap is 10 minutes, so a live session never outlives its token. |
| Format | opaque to the executor. A random 32-byte value stored hashed in VCL, or a signed token, is VCL's call. The executor never parses it. |
| Revocation | VCL may revoke it after `POST /session/result`, and should. |
| Scope | only the endpoints in this document. It is not a student login session and must not work on any other VCL route. |

## Tenant rules (the part the tests prove)

1. **No `founder_id` in any request.** Not in a path, not in a query string, not
   in a body, at any depth. The tenant is the token's `founder_id`, full stop.
   A request body containing a `founder_id` key anywhere is rejected with
   `400 founder_id_not_accepted`, even when the value matches the token. The
   executor client also refuses to send one, so this is enforced on both sides.
2. **Another tenant's row is a row that does not exist.** A lookup, audit target
   or evidence ref naming a row owned by a different founder answers exactly
   what a nonexistent id answers: `404 not_found`. The response must not say
   whether the row exists elsewhere. This matches the convention in
   `founder-os/founder_ledger/interface.py`.
3. **Rows come back with their `founder_id`**, as the interface returns them.
   The executor checks every returned row against the token's founder and
   aborts the session (`tenant_mismatch`) if one ever differs. That is a
   tripwire for a VCL bug, not a substitute for rule 2.
4. **Cross-tenant and destructive calls are not on this surface at all.**
   `export_corrected_labels`, `delete_founder`, `create_founder`,
   `update_founder_context`, and the Laya label calls (`add_label`,
   `correct_label`, `list_labels`) have no endpoint here. They belong to the
   dashboard, instructor and Laya job paths, which authenticate differently.
5. **Gate rules are VCL's to enforce**, through the same checks as the ledger
   interface (evidence refs owned by this founder, stage 3+ pass needs Sean's
   sign-off, fail needs `routes_to_stage`). A session token can never assert
   Sean's sign-off: VCL rejects `sean_signoff: true` or
   `decided_by: "claude+sean"` from a session token with `403 signoff_not_allowed`.

## Errors

Every error is JSON: `{ "error": "<code>", "message": "<human text>" }`.

| Status | Codes | Meaning |
|---|---|---|
| 400 | `bad_request`, `founder_id_not_accepted` | malformed body or query, or a `founder_id` was sent |
| 401 | `unauthorized` | missing or unknown token |
| 403 | `token_expired`, `token_revoked`, `signoff_not_allowed` | the token cannot do this |
| 404 | `not_found` | no such row for this token's founder (including rows that belong to someone else) |
| 422 | `ledger_error` | the ledger rejected the call (`LedgerError` in the interface: bad stage, bad evidence, a gate rule) |
| 5xx | any | VCL fault. The executor records it and does not retry writes. |

## Endpoints

Each maps to one method of `founder_ledger/interface.py`. Request and response
fields use the interface's snake_case names and row shapes: ids are UUID
strings, timestamps ISO-8601 UTC, JSON columns decoded, 0/1 columns booleans.

### Identity

| Method and path | Interface | Response |
|---|---|---|
| `GET /me` | none | `{ founder_id, session_id, expires_at }` for this token |

The executor calls `/me` before anything else and refuses the session with
`403 token_founder_mismatch` unless `founder_id` equals the one in the session
request.

### Founder and stage progress

| Method and path | Interface | Body / query | Response |
|---|---|---|---|
| `GET /founder` | `get_founder` | none | founder row |
| `GET /stage-progress` | `list_stage_progress` | none | `{ rows: [...] }` |
| `POST /stage-progress/{stage}/gate-pending` | `mark_gate_pending` | none | stage_progress row |

### Artifacts

| Method and path | Interface | Body / query | Response |
|---|---|---|---|
| `GET /artifacts` | `list_artifacts` | `?stage=&kind=` (both optional) | `{ rows: [...] }` |
| `GET /artifacts/{id}` | `get_artifact` | none | artifact row or 404 |
| `POST /artifacts` | `add_artifact` | `{ stage, kind, body, title?, meta? }` | 201, artifact row |

### Pains and interviews

| Method and path | Interface | Body / query | Response |
|---|---|---|---|
| `GET /pains` | `list_pains` | `?job=` (optional) | `{ rows: [...] }` |
| `POST /pains` | `add_pain` | `{ quote, source_url?, watering_hole?, segment?, job?, tags? }` | 201, pain row |
| `GET /interviews` | `list_interviews` | none | `{ rows: [...] }` |
| `POST /interviews` | `add_interview` | `{ interviewee, notes, conducted_on?, segment?, commitment?, earlyvangelist? }` | 201, interview row |

### Audits

| Method and path | Interface | Body / query | Response |
|---|---|---|---|
| `GET /audits` | `list_audits` | `?target_table=&target_id=` (optional) | `{ rows: [...] }` |
| `POST /audits` | `add_audit` | `{ target_table, target_id, auditor, check_name, verdict, findings? }` | 201, audit row. 404 if `target_id` is not this founder's row in `target_table`. |

### PR/FAQ

| Method and path | Interface | Body / query | Response |
|---|---|---|---|
| `GET /prfaq` | `list_prfaq_versions` | none | `{ rows: [...] }` |
| `GET /prfaq/latest` | `latest_prfaq` | none | row, or `null` with 200 when there is none |
| `POST /prfaq` | `add_prfaq_version` | `{ stage, body, assumptions? }` | 201, prfaq row |

### Gate decisions

| Method and path | Interface | Body / query | Response |
|---|---|---|---|
| `GET /gate-decisions` | `list_gate_decisions` | `?stage=` (optional) | `{ rows: [...] }` |
| `POST /gate-decisions` | `record_gate_decision` | `{ stage, gate_id, decision, decided_by, evidence, rationale, sean_signoff?, routes_to_stage? }` | 201, decision row. 404 if any evidence ref is not this founder's row. |

### Session result

| Method and path | Body | Response |
|---|---|---|
| `POST /session/result` | `{ session_id, status, reply?, error?, writes, usage? }` | 204 |

- `status` is `"completed"` or `"failed"`.
- `reply` is the coach's message to the founder, markdown, on completion.
- `error` is an error code on failure (`envelope_rejected`, `claude_failed`,
  `timeout`, `parse_error`, ...).
- `writes` is one entry per write the skill asked for, in order:
  `{ method, status: "applied" | "rejected" | "failed", id?, error? }`.
- `session_id` must equal the token's `session_id`, or VCL answers
  `403 token_founder_mismatch`.

VCL stores the reply against the session and shows it in the dashboard. It is
also the signal to revoke the token.

## What the executor lets a skill write

The executor does not give the model live access to this API. Claude runs with
`Read`, `Glob` and `Grep` over a scratch directory holding only the skill and
the stage's cards, and returns an envelope `{ reply, ledger_writes: [...] }`.
The executor validates the whole envelope, then applies the writes itself
through the token-scoped client. If any write in the envelope is invalid, none
are applied.

| Skill `type` | Writes it may request |
|---|---|
| `stage-skill`, `coach` | `add_artifact`, `add_pain`, `add_interview`, `add_prfaq_version`, `mark_gate_pending` |
| `auditor` | `add_audit`, `record_gate_decision` |

Extra executor-side rules, on top of VCL's:

- a write whose args contain a `founder_id` key at any depth rejects the envelope
- `add_artifact`, `add_prfaq_version`, `mark_gate_pending` and
  `record_gate_decision` must name the founder's current stage
- `record_gate_decision` is always sent with `decided_by: "claude"` and
  `sean_signoff: false`, so a stage 3+ pass cannot come from a session
- at most 25 writes per session

## Checklist for vibecodelisboa#508

- [ ] Mint and store session tokens (hashed), 15-minute TTL, bound to
      `founder_id` + `session_id`, usable only on `/api/founder-os/v1/ledger/*`.
- [ ] Every handler resolves `founder_id` from the token and passes it to the
      #506 adapter. No handler reads a `founder_id` from the request.
- [ ] Reject any body with a `founder_id` key at any depth (`400`).
- [ ] Cross-tenant ids answer `404 not_found`, same as nonexistent ones.
- [ ] Reject `sean_signoff: true` and `decided_by: "claude+sean"` from a
      session token (`403`).
- [ ] `POST /session/result` stores the reply and revokes the token.
- [ ] Port `founder-os/test/isolation.test.mjs` cases against the real routes
      with two seeded founders.
- [ ] Call the Worker's `POST /founder-os/sessions` with
      `FOUNDER_OS_TRIGGER_TOKEN` from the server side only, never from the
      browser.
