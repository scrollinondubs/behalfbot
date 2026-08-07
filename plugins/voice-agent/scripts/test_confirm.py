"""Pin the confirmation gate's fail-closed behaviour.

Every test in here is a way the gate could be talked past. The parser tests are
the cheap half; the state-machine tests are the half that matters, because the
question is not "does yes mean yes" but "what happens when the answer is
anything else at all".

    .venv/bin/python test_confirm.py
"""

from __future__ import annotations

import sys
import time

import confirm
from confirm import PendingConfirm, Verdict, parse_answer, resolve
from router import Route

FAILURES: list[str] = []


def check(name: str, got, want) -> None:
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}: got {got!r}, want {want!r}")
        FAILURES.append(name)


def fresh(prompt_done: bool = True, interrupted: bool = False) -> PendingConfirm:
    """A read-back that has finished speaking and is inside its window."""
    now = time.time()
    return PendingConfirm(
        utterance="what's on my GitHub queue",
        created=now - 1,
        prompt_done_at=now - 0.5 if prompt_done else None,
        interrupted=interrupted,
    )


def test_parser() -> None:
    print("\nparser")
    for text, want in [
        # the affirmatives that must work, because confirming has to be fast
        ("yes", Verdict.YES), ("yeah", Verdict.YES), ("yep", Verdict.YES),
        ("yup", Verdict.YES), ("go ahead", Verdict.YES), ("do it", Verdict.YES),
        ("send it", Verdict.YES), ("confirm", Verdict.YES),
        ("yes please", Verdict.YES), ("that's right", Verdict.YES),
        # refusals
        ("no", Verdict.NO), ("nope", Verdict.NO), ("nah", Verdict.NO),
        ("cancel", Verdict.NO), ("forget it", Verdict.NO),
        ("never mind", Verdict.NO), ("don't", Verdict.NO),
        ("drop it", Verdict.NO), ("stop", Verdict.NO),
        # naming Jill - drops the turn, and has to be as fast as confirming
        ("jill", Verdict.HELD), ("send it to jill", Verdict.HELD),
        ("no, jill instead", Verdict.HELD), ("privately", Verdict.HELD),
        # What Whisper actually does to "Jill". "jail" is not a guess: it is
        # what whisper-base produced for "send it to Jill" in every run of
        # bench_confirm, and before this list existed it parsed as consent.
        ("gil", Verdict.HELD), ("send it to jail", Verdict.HELD),
        ("jail", Verdict.HELD), ("gill", Verdict.HELD), ("jell", Verdict.HELD),
        # An unrecognised destination is never consent to the one Sean skipped.
        ("send it to him", Verdict.UNCLEAR),
        ("send that to my accountant", Verdict.UNCLEAR),
        ("send it to jax", Verdict.YES),
        # the collisions, and which way they have to fall
        ("yeah no", Verdict.NO),          # means no everywhere Sean has lived
        ("no yeah", Verdict.NO),          # means yes, but fails closed anyway
        ("yes, send that to jill", Verdict.HELD),
        # filler that is NOT consent
        ("ok", Verdict.UNCLEAR), ("okay", Verdict.UNCLEAR),
        ("sure", Verdict.UNCLEAR), ("right", Verdict.UNCLEAR),
        ("alright", Verdict.UNCLEAR), ("fine", Verdict.UNCLEAR),
        ("hmm", Verdict.UNCLEAR), ("uh", Verdict.UNCLEAR),
        # silence and noise
        ("", Verdict.UNCLEAR), ("   ", Verdict.UNCLEAR),
        ("[BLANK_AUDIO]", Verdict.UNCLEAR),
        # a new question that happens to contain a yes-word never counts
        ("yes I was wondering what the capital of Portugal is these days",
         Verdict.UNCLEAR),
        ("do it later when you get a chance to look at the other thing",
         Verdict.UNCLEAR),
        ("what's the weather like", Verdict.UNCLEAR),
    ]:
        check(f"parse {text!r}", parse_answer(text), want)


def test_happy_paths() -> None:
    print("\nverdicts under a valid read-back")
    check("yes escalates to Jax", resolve(fresh(), "yes").route, Route.JAX)
    check("yes consumes the turn", resolve(fresh(), "yes").passthrough, False)
    # Naming Jill no longer redirects anywhere. There is no Venice path in the
    # voice loop, so it drops the turn and hands it back to Sean.
    check("jill drops it", resolve(fresh(), "send it to jill").route, None)
    check("jill says so out loud", "Jill" in resolve(fresh(), "send it to jill").say, True)
    check("jill says so out loud",
          "Jill" in resolve(fresh(), "jill").say, True)
    check("no sends nothing", resolve(fresh(), "no").route, None)
    check("no consumes the turn", resolve(fresh(), "no").passthrough, False)


def test_fails_closed() -> None:
    """The whole point. Nothing but an explicit yes may reach Jax."""
    print("\nfail-closed")

    check("filler sends nothing", resolve(fresh(), "okay").route, None)
    check("filler is handled as a normal turn",
          resolve(fresh(), "okay").passthrough, True)
    check("silence sends nothing", resolve(fresh(), "").route, None)
    check("a new question sends nothing",
          resolve(fresh(), "what's the capital of Portugal").route, None)

    # A timed-out read-back cannot be revived by a late yes.
    stale = PendingConfirm(utterance="x",
                           created=time.time() - confirm.CONFIRM_TIMEOUT_SECS - 60,
                           prompt_done_at=time.time() - confirm.CONFIRM_TIMEOUT_SECS - 30)
    check("expired yes sends nothing", resolve(stale, "yes").route, None)
    check("expired is reported as expired", resolve(stale, "yes").reason, "expired")

    # Talking over the read-back means Sean did not hear what was about to go.
    if confirm.REQUIRE_FULL_PROMPT:
        check("barge-in yes sends nothing",
              resolve(fresh(interrupted=True), "yes").route, None)
        check("yes before the read-back finished sends nothing",
              resolve(fresh(prompt_done=False), "yes").route, None)

    # Every outcome that sends nothing must say so rather than going quiet.
    for answer in ["okay", "", "no", "what's the capital of Portugal"]:
        out = resolve(fresh(), answer)
        if out.route is None:
            check(f"{answer!r} is announced", bool(out.say.strip()), True)


def test_word_cap() -> None:
    print("\nlength cap")
    long_yes = "yes " + " ".join(["word"] * confirm.MAX_ANSWER_WORDS)
    check("a long utterance is never consent", parse_answer(long_yes), Verdict.UNCLEAR)
    check("the cap is enforced at the boundary",
          parse_answer(" ".join(["yes"] * confirm.MAX_ANSWER_WORDS)), Verdict.YES)


def test_prompt() -> None:
    print("\nread-back")
    line = confirm.prompt_line("what did I spend on the roof?")
    check("names the destination", "Jax" in line, True)
    check("quotes Sean back verbatim", "what did I spend on the roof" in line, True)
    check("offers all three answers",
          all(w in line.lower() for w in ("yes", "no", "jill")), True)
    # Truncating the read-back would mean confirming something other than what
    # gets sent, which is the one thing the gate cannot do.
    long = "x" * 600
    check("never truncates", long in confirm.prompt_line(long), True)


def test_deadline() -> None:
    print("\nwindow")
    now = time.time()
    p = PendingConfirm(utterance="x", created=now - 300)
    check("an unanswered read-back expires", p.expired(now), True)
    p.prompt_done_at = now
    check("the window starts when the read-back ends", p.expired(now), False)


def test_scope() -> None:
    """Off by default, and when on it fires on inferred routes only.

    The gate's privacy half went to the keyword net, which refuses outright
    rather than asking, so this is switched off unless someone turns it on. When
    it is on, Sean saying "Jax, what's on my queue" already names the
    destination and reading it back proves nothing; every other source is the
    router's guess.
    """
    print("\nscope")
    check("off unless switched on", confirm.needs_confirmation("model"), False)

    confirm.CONFIRM_ENABLED = True
    check("an explicit 'Jax, ...' is not read back",
          confirm.needs_confirmation("override"), False)
    for source in ("model", "sticky", "fallback"):
        check(f"an inferred route via {source} is read back",
              confirm.needs_confirmation(source), True)

    confirm.TRUST_EXPLICIT_OVERRIDE = False
    check("trusting the override can be switched off",
          confirm.needs_confirmation("override"), True)
    confirm.TRUST_EXPLICIT_OVERRIDE = True

    confirm.CONFIRM_ENABLED = False
    check("the master switch still wins", confirm.needs_confirmation("model"), False)


def test_delivery_is_off_by_default() -> None:
    """Nothing imported into a test or a benchmark may reach a human.

    This lives with the gate's tests because the gate is what caused it: an
    earlier revision notified Sean on every declined read-back, from a path the
    dry-run flag did not cover, and a three-minute benchmark run put eight
    notifications in front of him. The guard belongs on the delivery call rather
    than on any one feature, and this is the test that keeps it there.
    """
    print("\ndelivery guard")
    import escalation

    check("Discord is opt-in", escalation.DISCORD_ENABLED, False)
    check("a post from a test is refused",
          escalation._post_discord(escalation.ANSWER_CHANNEL or "0", "must not send"), False)
    check("a declined read-back notifies nobody",
          "discord" in escalation.record_confirmation_drop.__doc__.lower()
          and escalation.record_confirmation_drop("declined") is None, True)


if __name__ == "__main__":
    test_parser()
    test_happy_paths()
    test_fails_closed()
    test_word_cap()
    test_prompt()
    test_deadline()
    test_scope()
    test_delivery_is_off_by_default()
    print(f"\n{'FAILED: ' + ', '.join(FAILURES) if FAILURES else 'all pass'}")
    sys.exit(1 if FAILURES else 0)
