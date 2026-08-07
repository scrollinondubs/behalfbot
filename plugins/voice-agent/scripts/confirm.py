"""The confirmation gate on the Jax path.

Two of the spike's open problems turn out to be the same problem. The router
leaks private questions to Jax at a measured 4-12%, and the Jax path runs
`claude -p --dangerously-skip-permissions` on whatever Whisper thought it heard
with nobody watching. Both are the same missing step: nothing asks the operator before
his words leave the box and start an agent.

So one gate closes both. When a turn resolves to JAX the escalation does not
start. Instead the bot reads the transcript back with the destination named -
"Sending to Jax. what's on my GitHub queue. Say yes, no, or Jill." - and waits.
A misroute is now a question he answers in one word instead of a disclosure he
finds out about later, and a misheard sentence is something he hears before it
executes rather than after.

Design rules, all of them load-bearing:

**It fails closed.** Only an explicit affirmative sends. Silence, an ambiguous
reply, a timeout, a barge-in over the read-back, and any failure in here all end
with nothing sent. There is exactly one attempt - no re-asking - because a
retry loop hands a noisy room repeated chances to produce a stray "yeah".

**Declining is as fast as confirming.** "No" drops it. So does "Jill", which
since 2026-08-07 means "not this way" rather than "send it to Venice" - there is
no Venice path left in the voice loop, so naming her stops the turn and hands it
back to the operator. Both cost one word.

**The parse is deterministic.** No model runs here. A classifier deciding
whether "yeah no" was consent would put a probabilistic step inside the
authorisation, which is the thing the gate exists to remove, and it would add
its own latency to the one exchange that has to feel instant.

**Only the Jax path is gated.** CHAT never leaves the machine, and a refused
turn (HELD) was already stopped by the keyword net before anything reached here.

**And the gate is off by default.** See CONFIRM_ENABLED. Its privacy half was
made redundant by the keyword net refusing outright; what remains is hearing a
misheard sentence before it starts an agent, which is worth having and is not
worth paying for on every turn.

What this gate does NOT do is authenticate the speaker. See "Who can answer it"
in the README: a voice in the room that hears the prompt and says "yes" passes,
and nothing cheap available on this box changes that.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from enum import Enum

from router import JAX_NAMES, JILL_NAMES, Route

# Off by default since 2026-08-07.
#
# The gate had two jobs. Catching privacy misroutes is now the keyword net's,
# which refuses outright instead of asking, so that half is gone. What is left
# is letting the operator hear a misheard sentence before it starts an agent - real, but
# it costs a full read-back of every escalated turn, and a low-latency interface
# was the entire reason this thing exists. Available, not on.
CONFIRM_ENABLED = os.getenv("VOICE_CONFIRM_JAX", "0") != "0"
CONFIRM_TIMEOUT_SECS = float(os.getenv("VOICE_CONFIRM_TIMEOUT_SECS", "20"))
# An answer that arrives over the top of the read-back does not prove the operator heard
# what was about to be sent, so by default it does not count. Relaxing this
# makes the gate friendlier and weaker; the tradeoff is in the README.
REQUIRE_FULL_PROMPT = os.getenv("VOICE_CONFIRM_REQUIRE_FULL_PROMPT", "1") != "0"
# A confirmation is a short utterance. Anything longer is a new question that
# happens to contain the word "yes", and treating it as consent is how a gate
# gets talked past.
MAX_ANSWER_WORDS = int(os.getenv("VOICE_CONFIRM_MAX_ANSWER_WORDS", "8"))
# the operator naming the destination out loud is itself the authorisation, so the gate
# does not fire on it. The 8% private-question misroute this exists to catch
# lives entirely in the inferred path - the router guessing JAX for something
# the operator never addressed to Jax. When he opens with "Jax, ..." the read-back
# quotes his own sentence back at him, proves nothing he did not just say, and
# costs the full length of the utterance in latency before anything happens.
TRUST_EXPLICIT_OVERRIDE = os.getenv("VOICE_CONFIRM_TRUST_OVERRIDE", "1") != "0"


def needs_confirmation(decision) -> bool:
    """Whether this decision has to be read back before anything leaves the box.

    False unless the gate is switched on, and then true for any escalation the operator
    did not address by name. Accepts a Decision or, for older callers and tests,
    a bare ``decision.source`` string.
    """
    if not CONFIRM_ENABLED:
        return False
    source = decision if isinstance(decision, str) else getattr(decision, "source", "")
    return not (TRUST_EXPLICIT_OVERRIDE and source == "override")


class Verdict(str, Enum):
    YES = "YES"
    NO = "NO"
    # the operator named Jill. That is no longer a redirect - it drops the turn and
    # tells him to take it to her channel. The verdict is kept distinct from NO
    # so the spoken line can say which of the two happened.
    HELD = "HELD"
    UNCLEAR = "UNCLEAR"


# Checked in the order below, and the order matters more than the contents.
#
# JILL first: "no, send that to Jill" is a redirect, not a refusal, and reading
# it as a refusal would throw away the answer the operator asked for.
#
# NO before YES: "yeah no" means no everywhere the operator has ever lived, and every
# other collision between the two sets resolves the same way - the refusal is
# the later, more considered word.
_JILL = re.compile(rf"\b(?:{JILL_NAMES}|privately|private|venice)\b", re.I)
_NO = re.compile(
    r"\b(?:no|nope|nah|negative|cancel|stop|abort|don'?t|do not|forget it|"
    r"forget that|never ?mind|drop it|scratch that|leave it|not that one)\b", re.I)
_YES = re.compile(
    r"\b(?:yes|yeah|yep|yup|yes please|correct|confirm(?:ed|ing)?|affirmative|"
    r"go ahead|do it|send it|send that|send|that'?s right|please do)\b", re.I)

# Deliberately absent from _YES: "ok", "okay", "sure", "right", "fine", "alright".
# Every one of them is something a person says while thinking, and none of them
# is an answer to "yes, no, or Jill". Excluding them costs the operator a re-ask;
# including them would let a filler word start an agent run.

# "send it to <someone>" is the shape of a redirect, and _YES contains "send
# it". So a named destination is resolved before the affirmative is considered,
# and a destination that is not recognised fails closed rather than counting as
# consent to the destination the operator did not name. This is not hypothetical:
# whisper-base heard "send it to Jill" as "send it to jail", which matched
# "send it" and sent the question to Anthropic.
_DESTINATION = re.compile(r"\bto\s+(?:the\s+)?([a-z']+)", re.I)
_IS_JAX = re.compile(rf"^{JAX_NAMES}$", re.I)


def parse_answer(text: str) -> Verdict:
    """Classify a confirmation reply. Anything unrecognised is UNCLEAR."""
    text = (text or "").strip()
    if not text or len(text.split()) > MAX_ANSWER_WORDS:
        return Verdict.UNCLEAR
    if _JILL.search(text):
        return Verdict.HELD
    if _NO.search(text):
        return Verdict.NO
    dest = _DESTINATION.search(text)
    if dest and not _IS_JAX.match(dest.group(1)):
        return Verdict.UNCLEAR
    if _YES.search(text):
        return Verdict.YES
    return Verdict.UNCLEAR


@dataclass
class PendingConfirm:
    """One outstanding read-back, alive until answered or expired."""

    utterance: str
    created: float
    prompt_done_at: float | None = None
    interrupted: bool = False
    resolved: bool = False

    def deadline(self) -> float:
        """When this stops being answerable.

        Timed from the end of the read-back, not from its start, so a long
        utterance does not eat the window the operator has to answer in.
        """
        return (self.prompt_done_at or self.created) + CONFIRM_TIMEOUT_SECS

    def expired(self, now: float | None = None) -> bool:
        return (now or time.time()) > self.deadline()


@dataclass
class Outcome:
    """What to do with a turn that answered a pending confirmation."""

    route: Route | None      # escalate here, or None to send nothing
    say: str                 # spoken instead of, or in front of, the local reply
    passthrough: bool        # also handle this turn as an ordinary utterance
    reason: str              # for the log and the Discord breadcrumb


def prompt_line(utterance: str, private: bool = False) -> str:
    """The read-back. Names the destination, then quotes the operator back to himself.

    Spoken in full and never truncated: a summary of what is about to be sent is
    not the same as what is about to be sent, and the whole point is that the two
    match.

    ``private`` is the keyword-net case. It leads with why it is asking, because
    that is the only information the operator does not already have - he knows what he
    just said, and he did not ask for it to go anywhere in particular. Same
    three answers, so the parse is unchanged.
    """
    quoted = utterance.strip().rstrip(".?!,;:")
    if private:
        return f"That sounds private. {quoted}. Jill, or Jax?"
    return f"Sending to Jax. {quoted}. Say yes, no, or Jill."


def resolve(pending: PendingConfirm, answer: str, now: float | None = None) -> Outcome:
    """Decide what an answer to a pending confirmation means. Never raises."""
    now = now or time.time()
    verdict = parse_answer(answer)

    if REQUIRE_FULL_PROMPT and (pending.interrupted or pending.prompt_done_at is None):
        return Outcome(None, "You cut in, so I dropped that one. ", True, "interrupted")

    if pending.expired(now):
        return Outcome(None, "That one timed out, so I dropped it. ", True, "expired")

    if verdict is Verdict.YES:
        return Outcome(Route.JAX, "", False, "confirmed")
    if verdict is Verdict.HELD:
        return Outcome(None, "Not sending it. Take that one to Jill.", False, "held")
    if verdict is Verdict.NO:
        return Outcome(None, "Dropped it.", False, "declined")
    # Not an answer at all. Fail closed on the pending send and let the turn the operator
    # actually made be handled normally, with a word to say it did not go.
    return Outcome(None, "Dropped the last one. ", True, "unclear")
