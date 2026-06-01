# Welfare Check — SMS / call-cascade providers

The welfare cascade has to reach the installer's emergency contacts when monitoring trips. V1 reference uses Twilio bound to a specific account and toll-free number. The chassis abstracts that into the same provider-pluggable pattern as the LocationProvider work — each installer picks a backing service that fits their budget and region.

## The interface

```
plugins/angel-protocol/scripts/sms_providers/
├── __init__.py
├── base.py        # SmsProvider abstract class (send_sms, make_call, setup, health)
├── twilio.py      # V1 default — REST API via urllib (no twilio-python dep)
├── telnyx.py      # alt — cheaper outside US; stub for V1
├── aws_sns.py     # alt — SMS only, no voice calls; stub for V1
└── plivo.py       # alt — Twilio-API-compatible; stub for V1
```

Each provider implements:

```python
class SmsProvider:
    def send_sms(self, to: str, body: str, *, from_: Optional[str] = None) -> SendResult
    def make_call(self, to: str, twiml_url: str, *, from_: Optional[str] = None) -> SendResult
    def setup(self) -> int
    def health(self) -> HealthReport
```

`send_sms` and `make_call` return a `SendResult` typed dict — `{ok, provider_id, provider_message_id, cost_usd?, error?, raw?}`. Implementations must not raise on failure; the pager treats provider errors as cascade-relevant data, not exceptions.

## Provider matrix

| Provider | Setup difficulty | Cost (US-bound SMS) | Voice calls | Primary use case |
|---|---|---|---|---|
| **twilio** | Medium | ~$0.0079/SMS | Yes (TwiML) | Default; battle-tested for emergency comms; toll-free options |
| **telnyx** | Medium | ~$0.0035/SMS | Yes | Installers wanting cheaper non-US rates |
| **aws_sns** | Low (if AWS already in stack) | ~$0.00645/SMS | No | SMS-only fallback; tight per-region quotas |
| **plivo** | Medium | ~$0.0055/SMS | Yes | Twilio API-compatible; second-source for redundancy |

### Recommended default: `twilio`

V1 reference uses Twilio + a toll-free number (the canonical TF lives in the V1 install's reference memory). Twilio's emergency-comms reliability and toll-free flexibility make it the chassis V1 default. Stub providers cover the alternatives so the framework is clean for future implementations.

## Setup expectations

The chassis assumes provider credentials hydrate via the chassis secret store (Vaultwarden / 1Password / Bitwarden) into env vars at heartbeat-dispatcher startup time. For Twilio the relevant vars are:

- `TWILIO_ACCOUNT_SID`
- `TWILIO_AUTH_TOKEN`
- `TWILIO_PHONE_NUMBER` (default sender; per-cascade overrides via `emergency-contacts.json`)

Per-installer secret-store wiring is out of scope for this doc. See `docs/security.md` for the chassis secret-handoff pattern.

## Cost-control knobs

The pager invokes `provider.send_sms` and `provider.make_call` per emergency-contact per cascade. Cascade design is "fan out to all contacts in parallel" — so cost scales with contact count. A typical cascade is 3-6 contacts, ~$0.05 in worst case (Twilio with calls).

For testing, the pager defaults to dry-run; live mode is gated on `--live` flag AND `dry_run=false` in mode state. Two flags to clear before any real-world emergency-contact SMS goes out.

## Out of scope

- WhatsApp Business API: an option for installers in markets where SMS is rare. Defer until requested; same provider interface, different REST endpoints.
- Telegram / Signal: encrypted-by-default but not designed for cold-contact emergency cascade. Out of scope for welfare-check plugin.
- Discord webhooks: already used for the YELLOW-tier soft-alert path (`ANGEL_WEBHOOK_URL`); not appropriate for RED cascade because emergency contacts may not be Discord users.

## References

- <v1-reference-install>#510 — parent (sms-pager.py + cascade-test.py port)
- <v1-reference-install>#505 — Angel Phase 0 cross-platform port (parent of #510)
- behalfbot-chassis PR #12 — LocationProvider abstraction (mirror pattern)
