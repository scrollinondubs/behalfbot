"""AwsSnsProvider — stub. AWS SNS publish-to-phone-number API.

To implement:
1. Use boto3.client("sns") with credentials from AWS profile
2. sns.publish(PhoneNumber=to, Message=body)
3. SNS does NOT support outbound voice calls — calls return ok=False

Note: SNS has tight per-region SMS quotas (can be raised via AWS support).
Twilio is the safer default for emergency cascade.
"""

from __future__ import annotations

import sys
from typing import Optional

from .base import HealthReport, SendResult, SmsProvider


class AwsSnsProvider(SmsProvider):
    provider_id = "aws_sns"

    def send_sms(self, to: str, body: str, *, from_: Optional[str] = None) -> SendResult:
        return {"ok": False, "provider_id": self.provider_id, "error": "aws_sns provider not yet implemented"}

    def make_call(self, to: str, twiml_url: str, *, from_: Optional[str] = None) -> SendResult:
        return {"ok": False, "provider_id": self.provider_id, "error": "aws_sns does not support voice calls"}

    def setup(self) -> int:
        print("aws_sns provider not yet implemented", file=sys.stderr)
        return 1

    def health(self) -> HealthReport:
        return {"ok": False, "msg": "aws_sns not implemented", "auth_check": False, "balance_usd": None}
