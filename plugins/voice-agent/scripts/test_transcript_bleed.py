"""Regression test for the repeated-sentence transcript.

Sean saw every reply open by repeating the tail of the previous one. It was
never in the audio and never in the LLM context - both were verified clean over
a four-turn conversation. It was the browser transcript, and the cause is in
Pipecat's own RTVIObserver:

    self._bot_transcription += frame.text
    if match_endofsentence(self._bot_transcription):
        send(BotTranscriptionMessage(...)); self._bot_transcription = ""

Nothing clears that buffer when a turn ends. A reply whose final token carries
no terminal punctuation - which small local models emit constantly, e.g.
"that would be Lisbon" - strands its tail in the buffer, and the next reply's
transcript opens with it.

    .venv/bin/python test_transcript_bleed.py
"""

from __future__ import annotations

import asyncio
import sys

from pipecat.frames.frames import LLMFullResponseEndFrame, LLMTextFrame
from pipecat.observers.base_observer import FramePushed
from pipecat.processors.frameworks.rtvi import RTVIObserver, RTVIProcessor

from bot import TurnAwareRTVIObserver

# Two replies. The first has no terminal punctuation - that is the whole bug.
TURN_1 = ["that ", "would ", "be ", "Lisbon"]
TURN_2 = ["About ", "half ", "a ", "million ", "people ", "live ", "there."]


def collect(observer):
    """Capture BotTranscriptionMessage payloads instead of sending them."""
    seen: list[str] = []

    async def fake_send(model, exclude_none=True):
        if getattr(model, "type", None) == "bot-transcription":
            seen.append(model.data.text)

    observer.send_rtvi_message = fake_send
    return seen


async def replay(observer, flush_between: bool) -> list[str]:
    seen = collect(observer)
    for turn in (TURN_1, TURN_2):
        for token in turn:
            await observer._handle_llm_text_frame(LLMTextFrame(token))
        if flush_between:
            await observer.on_push_frame(FramePushed(
                source=None, destination=None, frame=LLMFullResponseEndFrame(),
                direction=None, timestamp=0))
    return seen


async def main() -> int:
    broken = await replay(RTVIObserver(RTVIProcessor()), flush_between=False)
    fixed = await replay(TurnAwareRTVIObserver(RTVIProcessor()), flush_between=True)

    print("upstream RTVIObserver, no turn-boundary flush:")
    for m in broken:
        print(f"  {m!r}")
    print("TurnAwareRTVIObserver:")
    for m in fixed:
        print(f"  {m!r}")

    ok = True

    # The bug: turn 1's text is stranded and reappears glued to turn 2.
    if not any(m.startswith("that would be Lisbon") and "live there." in m for m in broken):
        print("\nFAIL: could not reproduce the bleed against upstream")
        ok = False
    else:
        print("\nreproduced: upstream emits "
              f"{[m for m in broken if 'Lisbon' in m][0][:60]!r}")

    # The fix: two separate transcripts, and turn 2 does not carry turn 1.
    if len(fixed) != 2:
        print(f"FAIL: expected 2 transcript messages, got {len(fixed)}")
        ok = False
    elif "Lisbon" in fixed[1]:
        print(f"FAIL: turn 2 still carries turn 1: {fixed[1]!r}")
        ok = False
    else:
        print(f"fixed: turn 1 {fixed[0]!r}, turn 2 {fixed[1]!r} - no bleed")

    print("\nPASS" if ok else "\nFAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
