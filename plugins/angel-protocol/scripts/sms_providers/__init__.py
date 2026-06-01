"""Angel Protocol SMS / call-cascade providers.

Mirrors the LocationProvider abstraction in providers/__init__.py — each
SmsProvider implements a uniform send_sms / make_call interface so the
cascade pager scripts work against any backing service.

V1 reference uses Twilio. Chassis V1 ships:
- TwilioSmsProvider — primary
- TelnyxSmsProvider — alternative for installers with existing Telnyx
                      accounts (cheaper outside the US)
- Stub providers for AWS SNS, Plivo — interface-shaped, full implementations
                      land when an installer asks for them.

See docs/plugins/angel-protocol-sms-providers.md for the per-provider matrix
and rationale.
"""

from .base import SmsProvider

__all__ = ["SmsProvider", "load_provider"]


def load_provider(name: str) -> SmsProvider:
    """Look up an SMS provider by config name, return an instance."""
    if name == "twilio":
        from .twilio import TwilioSmsProvider
        return TwilioSmsProvider()
    if name == "telnyx":
        from .telnyx import TelnyxSmsProvider
        return TelnyxSmsProvider()
    if name == "aws_sns":
        from .aws_sns import AwsSnsProvider
        return AwsSnsProvider()
    if name == "plivo":
        from .plivo import PlivoSmsProvider
        return PlivoSmsProvider()
    raise ValueError(f"unknown sms provider: {name!r}")
