"""Pin the explicit-address layer.

The override is the one layer Sean can steer by hand, and it now also decides
whether the confirmation gate fires (see confirm.needs_confirmation). So a
vocative that fails to match is not just a routing miss - it is an unnecessary
read-back on a sentence where Sean already named the destination out loud.

Everything in here is a real whisper-base transcription of Sean addressing one
of the two names, or a sentence that must NOT be read as addressing either.

    .venv/bin/python test_override.py
"""

from __future__ import annotations

import sys

from router import Route, explicit_override

FAILURES: list[str] = []


def check(text: str, want: Route | None) -> None:
    got = explicit_override(text)
    if got == want:
        print(f"  ok   {text!r} -> {got}")
    else:
        print(f"  FAIL {text!r}: got {got}, want {want}")
        FAILURES.append(text)


def test_vocative() -> None:
    print("\nvocative, however Whisper punctuates it")
    check("Jax, what's on my GitHub queue?", Route.JAX)
    check("Jack, I'm testing the pipecat integration.", Route.JAX)
    # The one that regressed live: no comma, possessive tail.
    check("Jack's what's in my GitHub queue?", Route.JAX)
    check("Jacks what's in my GitHub queue", Route.JAX)
    check("Jax what's on my queue", Route.JAX)
    check("Jill, remind me what I said about the mortgage.", Route.HELD)
    check("Jill's the one who has that.", Route.HELD)


def test_addressed() -> None:
    print("\naddressed mid-sentence")
    check("ask Jax what's on my queue", Route.JAX)
    check("hey Jax can you check the deploy", Route.JAX)
    check("check with Jill about that one", Route.HELD)
    check("ask Jill, not Jax", Route.HELD)


def test_not_addressed() -> None:
    print("\nnot an address")
    check("what's on my GitHub queue?", None)
    check("how far is Lisbon from Tomar", None)
    # A name buried in the middle is not Sean addressing anyone.
    check("I was talking to Jack about the bikes", None)
    # A bare name with nothing after it is not an instruction.
    check("Jax", None)
    check("Jax.", None)


if __name__ == "__main__":
    test_vocative()
    test_addressed()
    test_not_addressed()
    print(f"\n{'FAILED: ' + ', '.join(FAILURES) if FAILURES else 'all pass'}")
    sys.exit(1 if FAILURES else 0)
