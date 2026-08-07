"""Pipecat processors that put the router inside the voice pipeline.

The naive placement - classify, then run the LLM - adds the classifier's full
latency to every single turn, including the ordinary conversation that is
supposed to stay at about a second. Measured on this box that is about 200 ms on
a 980 ms budget, for a decision that turns out to be CHAT nine times in ten.

So the work is split across two processors that straddle the LLM:

    aggregators.user() -> [RouteStart] -> llm -> [RouteGate] -> tts

``RouteStart`` sees the context frame, kicks off classification on a background
task, and forwards the frame immediately - so the local model starts generating
its answer while the router is still deciding. ``RouteGate`` holds the LLM's
text until the decision lands. If it is CHAT the text is released; if it is an
escalation the text is dropped unspoken and either the acknowledgement or the
Jax read-back goes out instead.

The cost of that speculation is a few hundred milliseconds of wasted local
generation on escalated turns. It buys back the whole classifier latency on
every chat turn, because TTS cannot start until a full sentence exists anyway,
and the route resolves before that. Nothing leaves the machine either way, so
generating a chat answer to a question that turns out to be private is wasted
compute and not a disclosure.

``RouteGate`` also owns the confirmation gate on the Jax path (`confirm.py`). A
JAX decision does not escalate; it reads the transcript back and waits. The next
turn is then two things at once - a possible answer to that read-back, and a
possible new question - so it is classified as normal and the gate decides which
it was. An answer that is not a clear yes, no or Jill falls through and is
handled as an ordinary utterance, with a word to say the pending send did not go.
"""

from __future__ import annotations

import asyncio
import os
import time

import httpx
from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InterruptionFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSSpeakFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

import confirm
import router
import escalation
from escalation import Escalator, record_confirmation_drop
from router import Decision, Route

# How long RouteGate will hold LLM text waiting for a decision before giving up.
# On timeout it escalates to JILL rather than releasing unclassified text, which
# is the same asymmetry the router itself applies.
GATE_TIMEOUT_SECS = float(os.getenv("VOICE_GATE_TIMEOUT_SECS", "3.0"))


class TurnState:
    """Shared between the two processors and the answer-delivery task."""

    def __init__(self, model: str, keep_turns: int = 6, model2: str = ""):
        self.model = model
        self.model2 = model2
        self.keep_turns = keep_turns
        self.pending: asyncio.Future[Decision] | None = None
        self.last_route: Route | None = None
        self.history: list[dict] = []
        self.bot_speaking = False
        self.user_speaking = False
        self.decisions: list[Decision] = []
        # An outstanding Jax read-back, and the line owed to Sean about the last
        # one that did not go through.
        self.pending_confirm: confirm.PendingConfirm | None = None
        self.drop_notice = ""
        self._watchdog: asyncio.Task | None = None
        self._client = httpx.AsyncClient()

    def record(self, role: str, content: str) -> None:
        self.history.append({"role": role, "content": content})
        del self.history[: max(0, len(self.history) - self.keep_turns * 2)]

    def drop_last_user(self) -> None:
        """Forget a turn that turned out to be a confirmation answer.

        A bare "yes" is not a subject, and leaving it in the routing history
        would make it the antecedent the next elliptical follow-up resolves
        against - which is exactly the context the stickiness layer depends on.
        """
        if self.history and self.history[-1]["role"] == "user":
            self.history.pop()

    @property
    def idle(self) -> bool:
        return not self.bot_speaking and not self.user_speaking

    async def aclose(self) -> None:
        self.close_confirmation()
        await self._client.aclose()

    async def classify(self, text: str) -> Decision:
        d = await router.route(
            text, self._client, model=self.model, model2=self.model2,
            history=self.history, last_route=self.last_route,
        )
        self.decisions.append(d)
        votes = " ".join(f"{m}={v.value if v else 'x'}" for m, v in d.votes.items())
        logger.info(f"ROUTE {d.route.value} via {d.source} in {d.latency_ms:.0f} ms "
                    f"[{votes}] - {text!r}")
        return d

    # --- the confirmation gate's half of the state ------------------------

    def open_confirmation(self, utterance: str, private: bool = False) -> str:
        """Arm a read-back and its timeout. Returns the line to speak."""
        self.close_confirmation()
        self.pending_confirm = confirm.PendingConfirm(
            utterance=utterance, created=time.time())
        self._watchdog = asyncio.create_task(self._expire(self.pending_confirm))
        return confirm.prompt_line(utterance, private=private)

    def close_confirmation(self) -> None:
        if self._watchdog is not None:
            self._watchdog.cancel()
            self._watchdog = None
        self.pending_confirm = None

    def note_confirm_drop(self, reason: str) -> None:
        """Log a blocked escalation locally. Nobody is notified - see escalation.py."""
        asyncio.create_task(asyncio.to_thread(record_confirmation_drop, reason))

    async def _expire(self, pending: confirm.PendingConfirm) -> None:
        """Fail closed on silence.

        The deadline moves once the read-back finishes speaking, so this polls
        rather than sleeping to a fixed wake-up.
        """
        try:
            while not pending.resolved:
                wait = pending.deadline() - time.time()
                if wait <= 0:
                    break
                await asyncio.sleep(min(wait, 1.0))
            if pending.resolved:
                return
            pending.resolved = True
            if self.pending_confirm is pending:
                self.pending_confirm = None
            self._watchdog = None
            logger.warning("CONFIRM expired - nothing sent to Jax")
            self.drop_notice = "That one timed out, so I dropped it. "
            await asyncio.to_thread(record_confirmation_drop, "expired")
        except asyncio.CancelledError:
            pass


def _latest_user_text(frame: LLMContextFrame) -> str:
    for message in reversed(frame.context.get_messages()):
        if isinstance(message, dict) and message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, list):
                return " ".join(p.get("text", "") for p in content if isinstance(p, dict)).strip()
            return str(content or "").strip()
    return ""


class RouteStart(FrameProcessor):
    """Kicks off classification, then gets out of the way."""

    def __init__(self, state: TurnState):
        super().__init__()
        self._state = state

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMContextFrame):
            text = _latest_user_text(frame)
            # Repair the transcript once, here, before anything reads it. The
            # router, the read-back and the escalation then all see the same
            # repaired sentence, so what Sean hears quoted back is what gets
            # sent. Doing it per-layer would let them disagree.
            text = router.normalize_transcript(text)
            if text:
                self._state.record("user", text)
                loop = asyncio.get_running_loop()
                self._state.pending = loop.create_future()

                async def go(t=text, fut=self._state.pending):
                    try:
                        fut.set_result(await self._state.classify(t))
                    except Exception as e:  # noqa: BLE001
                        if not fut.done():
                            fut.set_exception(e)

                asyncio.create_task(go())

        await self.push_frame(frame, direction)


class RouteGate(FrameProcessor):
    """Holds the model's answer until the route is known, then releases or replaces it."""

    def __init__(self, state: TurnState, escalator: Escalator):
        super().__init__()
        self._state = state
        self._esc = escalator
        self._buffer: list[LLMTextFrame] = []
        self._decision: Decision | None = None
        self._spoken: list[str] = []
        # Set once this turn's reply has been replaced by something of ours.
        # Everything the local model says afterwards is swallowed.
        self._replaced = False

    # --- helpers ----------------------------------------------------------

    def _reset(self) -> None:
        self._buffer.clear()
        self._decision = None
        self._spoken.clear()
        self._replaced = False

    async def _say(self, text: str) -> None:
        if not text:
            return
        self._spoken.append(text)
        await self.push_frame(LLMTextFrame(text), FrameDirection.DOWNSTREAM)

    def _take_notice(self) -> str:
        notice, self._state.drop_notice = self._state.drop_notice, ""
        return notice

    async def _flush_buffer(self):
        await self._say(self._take_notice())
        for f in self._buffer:
            self._spoken.append(f.text)
            await self.push_frame(f, FrameDirection.DOWNSTREAM)
        self._buffer.clear()

    async def _resolve(self) -> Decision:
        if self._decision is not None:
            return self._decision
        fut = self._state.pending
        if fut is None:
            self._decision = Decision(Route.CHAT, "fallback", 0.0)
            return self._decision
        try:
            self._decision = await asyncio.wait_for(asyncio.shield(fut), GATE_TIMEOUT_SECS)
        except Exception as e:  # noqa: BLE001 - unclassified text is never released
            # HELD, not JAX. The router failing tells us nothing about the
            # content, and the one thing that must not happen on an unknown
            # utterance is that it escalates by accident. Refusing costs Sean a
            # repeat.
            logger.error(f"route gate fell back to HELD: {type(e).__name__}: {e}")
            self._decision = Decision(Route.HELD, "fallback", 0.0, str(e))
        return self._decision

    async def _act_on(self, decision: Decision) -> None:
        """Release, gate, or escalate this turn once the route is known."""
        if decision.route is Route.CHAT:
            await self._flush_buffer()
            return

        self._buffer.clear()
        self._replaced = True
        utterance = self._state.history[-1]["content"] if self._state.history else ""

        # Refused. Nothing is sent, nothing is queued, and Sean is told so in
        # terms that make clear the next move is his. This is the only outcome
        # the keyword net can produce, which is what makes it safe to trust.
        if decision.route is Route.HELD:
            logger.info(f"HELD via {decision.source} - nothing sent - {utterance!r}")
            await self._say(self._take_notice() + escalation.HELD_LINE)
            return

        if confirm.needs_confirmation(decision):
            # Nothing leaves the box yet. Narrowed on 2026-08-07 to the cases
            # confirm.needs_confirmation still arms on.
            await self._say(self._take_notice() + self._state.open_confirmation(utterance))
            return

        ack = self._esc.start(decision.route, utterance)
        await self._say(self._take_notice() + ack)

    async def _answer_confirmation(self, pending: confirm.PendingConfirm) -> bool:
        """Read this turn as an answer to a pending read-back.

        Returns True when the turn was consumed by the gate. False means the
        answer was not a yes, a no or a redirect, so the pending send is dropped
        and the turn continues as an ordinary utterance.
        """
        answer = self._state.history[-1]["content"] if self._state.history else ""
        outcome = confirm.resolve(pending, answer)
        pending.resolved = True
        self._state.close_confirmation()
        logger.info(f"CONFIRM {outcome.reason} -> "
                    f"{outcome.route.value if outcome.route else 'nothing sent'} "
                    f"- answer {answer!r}")

        if outcome.route is None:
            self._state.note_confirm_drop(outcome.reason)

        if outcome.passthrough:
            self._state.drop_notice = outcome.say
            return False

        self._state.drop_last_user()
        self._replaced = True
        line = outcome.say
        if outcome.route is not None:
            ack = self._esc.start(outcome.route, pending.utterance)
            line = outcome.say or ack
        await self._say(line)
        return True

    # --- the frame path ---------------------------------------------------

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        pending = self._state.pending_confirm

        if isinstance(frame, UserStartedSpeakingFrame):
            self._state.user_speaking = True
        elif isinstance(frame, UserStoppedSpeakingFrame):
            self._state.user_speaking = False
        elif isinstance(frame, BotStartedSpeakingFrame):
            self._state.bot_speaking = True
            if pending is not None:
                pending.prompt_started = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._state.bot_speaking = False
            if pending is not None and pending.prompt_started:
                # Kokoro emits a start/stop pair per *sentence*, so the first
                # stop is the end of the first sentence, not the end of the
                # read-back. Setting the mark once made the answer window start
                # while the bot was still talking: a ten second read-back ate
                # half of a twenty second window and the gate expired on Sean
                # mid-thought. Keep pushing the mark forward instead - the last
                # stop before the answer is the real end of the prompt.
                pending.prompt_done_at = time.time()

        if isinstance(frame, InterruptionFrame):
            # Talking over the read-back means Sean did not hear all of what was
            # about to be sent, which is the one thing the gate exists to
            # guarantee. Marked here, refused when the answer arrives.
            #
            # Keyed off the bot actually speaking rather than off prompt_done_at
            # being unset, because that mark now advances on every sentence
            # boundary. Pipecat only raises this frame when the user cuts in
            # while the bot has the floor, and the gaps between sentences are
            # sub-millisecond, so this catches the same barge-ins as before.
            if pending is not None and self._state.bot_speaking:
                pending.interrupted = True
            self._reset()
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, LLMFullResponseStartFrame):
            self._reset()
            await self.push_frame(frame, direction)
            if pending is not None and not pending.resolved:
                await self._answer_confirmation(pending)
            return

        if isinstance(frame, LLMTextFrame) and direction == FrameDirection.DOWNSTREAM:
            if self._replaced:
                return  # our line already went out; swallow the local generation
            if self._decision is None:
                self._buffer.append(frame)
                await self._act_on(await self._resolve())
                return
            if self._decision.route is Route.CHAT:
                self._spoken.append(frame.text)
                await self.push_frame(frame, direction)
            return

        if isinstance(frame, LLMFullResponseEndFrame):
            if self._buffer:
                await self._act_on(await self._resolve())
            # A turn the gate consumed never reaches _resolve, so _decision is
            # None and the stickiness state correctly does not move for a "yes".
            if self._decision is not None:
                self._state.last_route = self._decision.route
            if self._spoken:
                self._state.record("assistant", "".join(self._spoken))
            self._spoken.clear()

        await self.push_frame(frame, direction)


async def deliver_answers(state: TurnState, escalator: Escalator, task,
                          idle_secs: float = 1.0, max_wait: float = 25.0):
    """Speak escalation answers once the room goes quiet.

    Discord delivery has already happened by the time an answer reaches this
    queue, so waiting for a gap is safe: the answer is never lost, only spoken
    late or not at all. Cutting across Sean mid-sentence to read out something
    he asked for a minute ago is worse than staying silent.
    """
    while True:
        esc = await escalator.answers.get()
        waited = 0.0
        while not state.idle and waited < max_wait:
            await asyncio.sleep(idle_secs)
            waited += idle_secs
        if not state.idle:
            logger.info(f"{esc.route.value} answer not spoken - never went idle; Discord has it")
            continue
        line = Escalator.spoken_form(esc)
        state.record("assistant", line)
        await task.queue_frames([TTSSpeakFrame(line)])
        logger.info(f"spoke {esc.route.value} answer after {esc.elapsed:.1f}s")
