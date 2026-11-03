#!/usr/bin/env python3
"""The `tripwires` heartbeat: run every due tripwire, post what needs posting.

Posts for itself, so the model never runs in normal operation: stdout is
{"count": 0, ...}. count is 1 only when the heartbeat itself is broken (the
store cannot be read, or a message could not be sent), so the dispatcher's
repeat-alert handling gets a chance to tell someone. Nothing from a fetched
page reaches this output. An unexpected exception exits non-zero, which the
dispatcher reports for a critical heartbeat.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _tripwires as tw  # noqa: E402


def broken(reason: str) -> dict:
    return {"count": 1, "reason": reason, "alert_signature": reason}


def main() -> int:
    try:
        summary = tw.run_due(tw.Store())
    except tw.StoreBusy:
        out = {"count": 0, "reason": "store_busy"}
    except tw.StoreUnavailable:
        out = broken("store_unreadable")
    else:
        out = broken("send_failed") if summary["send_failed"] else {"count": 0}
        out.update(summary)
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
