"""SmsProvider abstract base class.

Every SMS / call-cascade provider implements this interface. The cascade
pager script (sms-pager.py — port deferred per <v1-reference-install>#510)
instantiates the provider named in
chassis.config.yaml.modules.angel_protocol.sms_provider and calls through.
"""

from __future__ import annotations

import abc
from typing import Optional, TypedDict


class SendResult(TypedDict, total=False):
    ok: bool
    provider_id: str
    provider_message_id: Optional[str]
    cost_usd: Optional[float]
    error: Optional[str]
    raw: dict  # provider-specific raw response


class HealthReport(TypedDict, total=False):
    ok: bool
    msg: str
    auth_check: bool
    balance_usd: Optional[float]


class SmsProvider(abc.ABC):
    """Abstract interface for an SMS / phone-call cascade backend."""

    #: Short id matching the chassis.config.yaml provider name. Subclasses set this.
    provider_id: str = ""

    @abc.abstractmethod
    def send_sms(self, to: str, body: str, *, from_: Optional[str] = None) -> SendResult:
        """Send an SMS to the given E.164 number. Return a SendResult.

        Implementations must NOT raise on failure — return ok=False with an
        error message instead. The pager treats provider failures as
        cascade-relevant data, not exceptions.
        """

    @abc.abstractmethod
    def make_call(self, to: str, twiml_url: str, *, from_: Optional[str] = None) -> SendResult:
        """Initiate a phone call to the given number; on connect, the
        provider fetches twiml_url and reads the resulting message.

        Twilio-style API. Providers that don't natively support TwiML can
        implement an equivalent via their own announcement primitive.
        """

    @abc.abstractmethod
    def setup(self) -> int:
        """Run interactive first-run setup — verify credentials, write
        provider-specific config, return 0 on success.
        """

    @abc.abstractmethod
    def health(self) -> HealthReport:
        """Liveness check. Should hit a cheap auth endpoint (e.g. account
        balance) so the ops watchdog knows the credentials still work.
        """
