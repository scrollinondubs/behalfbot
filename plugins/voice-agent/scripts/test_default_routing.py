"""The 2026-08-07 routing reversal, pinned.

Four things changed together and each of them can silently undo the others, so
they are checked in one place:

  1. transcription repair, so the router sees the sentence Sean spoke
  2. leading filler stripped, so an address is not lost behind "Alright,"
  3. JAX as the default, so an unaddressed question does not go to Venice
  4. the keyword net asking instead of rerouting

Run: .venv/bin/python test_default_routing.py
"""

from __future__ import annotations

import router
import escalation
from confirm import needs_confirmation
from router import Decision, Route

FAILURES: list[str] = []


def _held_raises() -> bool:
    """start() must refuse a HELD route loudly rather than escalate it."""
    try:
        escalation.Escalator().start(Route.HELD, "what did the biopsy say")
    except ValueError:
        return True
    except Exception:
        return False
    return False


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {label}: {got!r}")
    if not ok:
        FAILURES.append(f"{label}: got {got!r}, want {want!r}")


print("transcription repair")
# The live 2026-08-07 failure, end to end: Whisper wrote "Jack" and "C1", the
# override never fired, and a question Sean addressed to Jax landed on Jill.
spoken = "Alright, Jack, can you tell me in C1 what my top priorities are"
repaired = router.normalize_transcript(spoken)
check("Jack -> Jax and C1 -> SiYuan", repaired,
      "Alright, Jax, can you tell me in SiYuan what my top priorities are")
check("that utterance now routes to Jax", router.explicit_override(repaired), Route.JAX)
check("unknown words are left alone",
      router.normalize_transcript("what is on my github queue"),
      "what is on my github queue")
check("no substitution inside a longer word",
      router.normalize_transcript("jackson called"), "jackson called")

print("\nleading filler")
for opener in ("Alright, ", "So ", "Okay, ", "Well ", "Hey ", ""):
    check(f"{opener!r} + vocative",
          router.explicit_override(f"{opener}Jax, what is on my queue"), Route.JAX)
check("filler alone is not an address",
      router.explicit_override("alright then"), None)
check("Jill still wins over Jax when both are named",
      router.explicit_override("Jax, actually ask Jill about this"), Route.HELD)

print("\ndefaults")
check("default route is JAX", router.DEFAULT_ROUTE, Route.JAX)
check("a model JILL vote is demoted", router._apply_default(Route.HELD), Route.JAX)
check("a model CHAT vote is untouched", router._apply_default(Route.CHAT), Route.CHAT)
check("no verdict at all takes the default", router._apply_default(None), Route.JAX)

print("\nthe keyword net still fires")
check("tax question trips it",
      router.sensitive_prefilter("what did I pay in VAT last quarter"), True)
check("medical question trips it",
      router.sensitive_prefilter("what did the doctor say"), True)
check("a work question does not",
      router.sensitive_prefilter("what is on my github queue"), False)

print("\nthe keyword net refuses rather than routes")
check("a private utterance is HELD", router._apply_default(Route.HELD), Route.JAX)
check("the read-back gate is off by default",
      needs_confirmation(Decision(Route.JAX, "model", 0.0)), False)
check("naming Jill holds the turn",
      router.explicit_override("ask Jill what I paid in VAT"), Route.HELD)
check("HELD is never escalatable", _held_raises(), True)

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURES")
    for f in FAILURES:
        print(f"  {f}")
    raise SystemExit(1)
print("all pass")
