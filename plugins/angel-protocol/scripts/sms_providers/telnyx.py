"""TelnyxSmsProvider — alternative to Twilio (cheaper outside US).

Stub. Telnyx has a similar REST API to Twilio but uses Bearer auth instead
of HTTP Basic. Implementing this provider is straightforward (the V1
reference team has a separate Telnyx-using install path) — defer until an
installer asks for it.

To implement:
1. Read TELNYX_API_KEY from env
2. POST https://api.telnyx.com/v2/messages with body {to, from, text}
3. Auth: `Authorization: Bearer <key>`
4. Calls: POST /v2/calls with {to, from, audio_url, ...}

Provider author guide: see plugins/angel-protocol/scripts/sms_providers/twilio.py
for the canonical implementation pattern (urllib + urlencode + clean error
returns).
"""

from __future__ import annotations

import sys
from typing import Optional

from .base import HealthReport, SendResult, SmsProvider


class TelnyxSmsProvider(SmsProvider):
    provider_id = "telnyx"

    def send_sms(self, to: str, body: str, *, from_: Optional[str] = None) -> SendResult:
        return {"ok": False, "provider_id": self.provider_id, "error": "telnyx provider not yet implemented"}

    def make_call(self, to: str, twiml_url: str, *, from_: Optional[str] = None) -> SendResult:
        return {"ok": False, "provider_id": self.provider_id, "error": "telnyx provider not yet implemented"}

    def setup(self) -> int:
        print("telnyx provider not yet implemented", file=sys.stderr)
        return 1

    def health(self) -> HealthReport:
        return {"ok": False, "msg": "telnyx not implemented", "auth_check": False, "balance_usd": None}
