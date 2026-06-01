"""TwilioSmsProvider — chassis V1 default SMS / call provider.

Uses Twilio's REST API directly via urllib (no twilio-python dependency —
chassis aims for minimal dependencies). Same auth model as the V1 reference
in <v1-reference-install>/scripts/angel-sms-pager.py: HTTP Basic Auth with
TWILIO_ACCOUNT_SID:TWILIO_AUTH_TOKEN, POSTs to /Messages.json or /Calls.json.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .base import HealthReport, SendResult, SmsProvider


def _env(key: str) -> Optional[str]:
    return os.environ.get(key)


class TwilioSmsProvider(SmsProvider):
    provider_id = "twilio"

    def _auth_header(self) -> Optional[str]:
        sid = _env("TWILIO_ACCOUNT_SID")
        token = _env("TWILIO_AUTH_TOKEN")
        if not sid or not token:
            return None
        return "Basic " + base64.b64encode(f"{sid}:{token}".encode()).decode()

    def _from_default(self, override: Optional[str]) -> Optional[str]:
        return override or _env("TWILIO_PHONE_NUMBER")

    def send_sms(self, to: str, body: str, *, from_: Optional[str] = None) -> SendResult:
        sid = _env("TWILIO_ACCOUNT_SID")
        auth = self._auth_header()
        from_num = self._from_default(from_)
        if not sid or not auth:
            return {"ok": False, "provider_id": self.provider_id,
                    "error": "TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN not set"}
        if not from_num:
            return {"ok": False, "provider_id": self.provider_id,
                    "error": "TWILIO_PHONE_NUMBER not set and no from_ override"}

        url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
        data = urlencode({"To": to, "From": from_num, "Body": body}).encode()
        req = Request(url, data=data, method="POST")
        req.add_header("Authorization", auth)
        req.add_header("Content-Type", "application/x-www-form-urlencoded")

        try:
            with urlopen(req, timeout=10) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                return {
                    "ok": True,
                    "provider_id": self.provider_id,
                    "provider_message_id": payload.get("sid"),
                    "raw": payload,
                }
        except HTTPError as e:
            try:
                err_body = e.read().decode("utf-8", errors="replace")
            except Exception:
                err_body = ""
            return {"ok": False, "provider_id": self.provider_id,
                    "error": f"HTTP {e.code}: {err_body}"}
        except URLError as e:
            return {"ok": False, "provider_id": self.provider_id, "error": f"URL error: {e}"}
        except Exception as e:
            return {"ok": False, "provider_id": self.provider_id, "error": f"unexpected: {e}"}

    def make_call(self, to: str, twiml_url: str, *, from_: Optional[str] = None) -> SendResult:
        sid = _env("TWILIO_ACCOUNT_SID")
        auth = self._auth_header()
        from_num = self._from_default(from_)
        if not sid or not auth:
            return {"ok": False, "provider_id": self.provider_id,
                    "error": "TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN not set"}
        if not from_num:
            return {"ok": False, "provider_id": self.provider_id,
                    "error": "TWILIO_PHONE_NUMBER not set and no from_ override"}

        url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls.json"
        data = urlencode({"To": to, "From": from_num, "Url": twiml_url}).encode()
        req = Request(url, data=data, method="POST")
        req.add_header("Authorization", auth)
        req.add_header("Content-Type", "application/x-www-form-urlencoded")

        try:
            with urlopen(req, timeout=10) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                return {
                    "ok": True,
                    "provider_id": self.provider_id,
                    "provider_message_id": payload.get("sid"),
                    "raw": payload,
                }
        except HTTPError as e:
            try:
                err_body = e.read().decode("utf-8", errors="replace")
            except Exception:
                err_body = ""
            return {"ok": False, "provider_id": self.provider_id,
                    "error": f"HTTP {e.code}: {err_body}"}
        except URLError as e:
            return {"ok": False, "provider_id": self.provider_id, "error": f"URL error: {e}"}
        except Exception as e:
            return {"ok": False, "provider_id": self.provider_id, "error": f"unexpected: {e}"}

    def setup(self) -> int:
        sid = _env("TWILIO_ACCOUNT_SID")
        token = _env("TWILIO_AUTH_TOKEN")
        if not sid or not token:
            print(
                "TwilioSmsProvider.setup: set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN "
                "in the chassis .env (hydrate from password manager). Optionally set "
                "TWILIO_PHONE_NUMBER for the default sender; per-cascade entries can "
                "override via emergency-contacts.json.",
                file=sys.stderr,
            )
            return 1
        # Verify creds by hitting the Account endpoint
        url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}.json"
        req = Request(url, method="GET")
        req.add_header("Authorization", self._auth_header() or "")
        try:
            with urlopen(req, timeout=10) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                friendly = payload.get("friendly_name", "(unknown)")
                print(f"TwilioSmsProvider: auth OK — account '{friendly}' (status: {payload.get('status')})")
                return 0
        except Exception as e:
            print(f"TwilioSmsProvider.setup: auth check failed — {e}", file=sys.stderr)
            return 1

    def health(self) -> HealthReport:
        sid = _env("TWILIO_ACCOUNT_SID")
        if not sid or not self._auth_header():
            return {"ok": False, "msg": "creds not set", "auth_check": False, "balance_usd": None}
        url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Balance.json"
        req = Request(url, method="GET")
        req.add_header("Authorization", self._auth_header() or "")
        try:
            with urlopen(req, timeout=5) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                bal = float(payload.get("balance", 0)) if payload.get("balance") else None
                return {"ok": True, "msg": f"balance ${bal}", "auth_check": True, "balance_usd": bal}
        except Exception as e:
            return {"ok": False, "msg": f"balance check failed: {e}", "auth_check": False, "balance_usd": None}
