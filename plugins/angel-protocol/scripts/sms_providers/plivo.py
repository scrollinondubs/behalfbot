"""PlivoSmsProvider — stub.

Plivo is another Twilio-compatible alternative; their REST API is close
enough that a port from twilio.py is mostly endpoint URL changes + auth
header tweaks. Defer until an installer asks.
"""

from __future__ import annotations

import sys
from typing import Optional

from .base import HealthReport, SendResult, SmsProvider


class PlivoSmsProvider(SmsProvider):
    provider_id = "plivo"

    def send_sms(self, to: str, body: str, *, from_: Optional[str] = None) -> SendResult:
        return {"ok": False, "provider_id": self.provider_id, "error": "plivo provider not yet implemented"}

    def make_call(self, to: str, twiml_url: str, *, from_: Optional[str] = None) -> SendResult:
        return {"ok": False, "provider_id": self.provider_id, "error": "plivo provider not yet implemented"}

    def setup(self) -> int:
        print("plivo provider not yet implemented", file=sys.stderr)
        return 1

    def health(self) -> HealthReport:
        return {"ok": False, "msg": "plivo not implemented", "auth_check": False, "balance_usd": None}
