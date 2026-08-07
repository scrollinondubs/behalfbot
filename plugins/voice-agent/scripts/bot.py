"""Local low-latency voice agent - spike.

Everything except the LLM runs in this process on the Apple Silicon GPU. The LLM
is whatever OpenAI-compatible server VOICE_LLM_BASE_URL points at (Ollama by
default). Serves the Pipecat prebuilt web client and the WebRTC signaling
endpoint from one origin so a single `tailscale serve` mapping covers both.

Pipeline modeled on kwindla/macos-local-voice-agents, ported to Pipecat 1.7.
"""

import argparse
import asyncio
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Request
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.observers.user_bot_latency_observer import UserBotLatencyObserver
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.frames.frames import LLMFullResponseEndFrame
from pipecat.processors.frameworks.rtvi import RTVIObserver, RTVIProcessor
from pipecat.processors.frameworks.rtvi import models as RTVI_MODELS
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.whisper.stt import WhisperSTTServiceMLX
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.connection import IceServer, SmallWebRTCConnection
from pipecat.transports.smallwebrtc.request_handler import (
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat_ai_small_webrtc_prebuilt.frontend import SmallWebRTCPrebuiltUI

import confirm
import escalation
import router as router_mod
from escalation import Escalator
from pipeline_router import RouteGate, RouteStart, TurnState, deliver_answers
from tts_mlx import KokoroMLXTTSService, get_worker

# Scoped to voice/.env on purpose. A bare load_dotenv() walks up and pulls in the
# repo-root .env, which has nothing to do with this spike.
load_dotenv(Path(__file__).parent / ".env", override=True)

LLM_BASE_URL = os.getenv("VOICE_LLM_BASE_URL", "http://127.0.0.1:11434/v1")
LLM_MODEL = os.getenv("VOICE_LLM_MODEL", "llama3.2:3b")
# base is ~10x faster than large-v3-turbo-q4 (79ms vs 821ms on a 3.3s clip) and
# STT is the single biggest term in the latency budget. See bench_stt_models.py.
STT_MODEL = os.getenv("VOICE_STT_MODEL", "mlx-community/whisper-base-mlx")
TTS_VOICE = os.getenv("VOICE_TTS_VOICE", "af_heart")
VAD_STOP_SECS = float(os.getenv("VOICE_VAD_STOP_SECS", "0.2"))
# A model of its own, not the conversational one. Two roles on one model share a
# slot in Ollama, so their prompt prefixes evict each other and every
# classification pays full prompt evaluation again - measured at 150 ms per call
# versus 29 ms with a dedicated model. Separate models also get separate runner
# processes, which is what lets the classification actually overlap the answer
# instead of queueing behind it.
ROUTER_MODEL = os.getenv("VOICE_ROUTER_MODEL", "gemma3:4b")
ROUTING_ENABLED = os.getenv("VOICE_ROUTING", "1") != "0"
# The second voter, read through router.py so the "off" spellings are shared.
# Its JILL vote counts on its own; the union is what the boundary rests on.
ROUTER_MODEL_2 = router_mod.second_model(router_mod.ROUTER_MODEL_2)

METRICS_PATH = Path(__file__).parent / "metrics" / "latency.jsonl"

# What this model is, and is not, is load-bearing. Phase 1 ran it with an empty
# system prompt and Sean got a bot that claimed to have Jax's tools, memory and
# "creators". It is the front door, not the assistant: it handles the small talk
# and hands everything else to something that can actually do the work.
SYSTEM_INSTRUCTION = """You are the voice front end for Jax, Sean's assistant.

You are not Jax. You have no tools, no memory of past sessions, no access to
Sean's files, repos, calendar, email or accounts, and no knowledge of his
business beyond what he says to you right now. Anything that needs those is
handed to Jax or to Jill automatically, without you doing anything - you will
never be asked to answer one of those. Never claim or imply you can look
something up, check something, or remember something from before.

So answer what you actually can: general knowledge, facts about the world,
directions, definitions, and conversation. If you do not know something, say so
plainly in one sentence.

Your input is text transcribed in realtime from Sean's voice, so expect
transcription errors and adjust without commenting on them.

Your output is converted to speech. Never use markdown, lists, emoji, or special
characters. Write plain spoken sentences and end every sentence with a full
stop. Keep answers to one or two short sentences unless asked for detail.

Open the conversation by saying "Hey, I'm Jax. What's up?" then stop and wait.
"""


class TurnAwareRTVIObserver(RTVIObserver):
    """RTVIObserver that does not bleed one reply's transcript into the next.

    Upstream builds the `bot-transcription` message by concatenating LLM text and
    flushing only when the buffer matches end-of-sentence punctuation. Nothing
    clears that buffer at a turn boundary. A reply whose last token carries no
    terminal punctuation - which small local models produce constantly, e.g.
    "that would be Lisbon" - leaves its tail sitting in the buffer, and the next
    reply's transcript opens with it.

    That is the repeated-sentence transcript Sean saw. It was only ever on
    screen: the spoken audio and the LLM context were both verified clean across
    a four-turn conversation (bench_conversation.py, and VOICE_DEBUG_CONTEXT=1).

    Flushing whatever is pending when the LLM response ends closes it.
    """

    async def on_push_frame(self, data):
        await super().on_push_frame(data)
        if isinstance(data.frame, LLMFullResponseEndFrame) and self._bot_transcription:
            pending, self._bot_transcription = self._bot_transcription, ""
            await self.send_rtvi_message(
                RTVI_MODELS.BotTranscriptionMessage(
                    data=RTVI_MODELS.TextMessageData(text=pending)
                )
            )


def make_latency_recorder(session_id: str):
    """Append one JSON record per completed user-to-bot turn."""
    METRICS_PATH.parent.mkdir(parents=True, exist_ok=True)
    state = {"total": None}

    async def on_latency_measured(observer, latency_seconds: float):
        state["total"] = latency_seconds

    async def on_latency_breakdown(observer, breakdown):
        record = {
            "ts": time.time(),
            "session": session_id,
            "llm_model": LLM_MODEL,
            "stt_model": STT_MODEL,
            "vad_stop_secs": VAD_STOP_SECS,
            "voice_to_voice_secs": state["total"],
            "user_turn_secs": breakdown.user_turn_secs,
            "ttfb": {t.processor: round(t.duration_secs, 4) for t in breakdown.ttfb},
        }
        if breakdown.text_aggregation:
            record["text_aggregation_secs"] = round(
                breakdown.text_aggregation.duration_secs, 4
            )
        with METRICS_PATH.open("a") as f:
            f.write(json.dumps(record) + "\n")
        logger.info(
            "TURN v2v={} endpoint={} {}".format(
                f"{state['total']:.3f}s" if state["total"] is not None else "n/a",
                f"{breakdown.user_turn_secs:.3f}s" if breakdown.user_turn_secs else "n/a",
                " ".join(f"{t.processor}={t.duration_secs:.3f}s" for t in breakdown.ttfb),
            )
        )
        state["total"] = None

    return on_latency_measured, on_latency_breakdown


async def run_bot(webrtc_connection: SmallWebRTCConnection):
    transport = SmallWebRTCTransport(
        webrtc_connection=webrtc_connection,
        params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
    )

    # Pipecat 1.7 dropped vad_analyzer/turn_analyzer from TransportParams. They are
    # pydantic-ignored if you pass them, so the bot runs with no VAD at all and
    # never hears anything. VAD is now a pipeline processor; smart-turn v3 is
    # already the default user-turn stop strategy on the user aggregator.
    vad = VADProcessor(vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=VAD_STOP_SECS)))

    stt = WhisperSTTServiceMLX(settings=WhisperSTTServiceMLX.Settings(model=STT_MODEL))
    tts = KokoroMLXTTSService(voice=TTS_VOICE, sample_rate=24000)
    llm = OpenAILLMService(
        api_key="ollama-local-no-key",
        model=LLM_MODEL,
        base_url=LLM_BASE_URL,
    )

    context = LLMContext([{"role": "system", "content": SYSTEM_INSTRUCTION}])
    aggregators = LLMContextAggregatorPair(context)
    rtvi = RTVIProcessor()

    latency = UserBotLatencyObserver()
    measured, breakdown = make_latency_recorder(webrtc_connection.pc_id)
    latency.add_event_handler("on_latency_measured", measured)
    latency.add_event_handler("on_latency_breakdown", breakdown)

    state = TurnState(model=ROUTER_MODEL, model2=ROUTER_MODEL_2 or "off")
    escalator = Escalator()

    stages = [transport.input(), vad, stt, rtvi, aggregators.user()]
    if ROUTING_ENABLED:
        stages += [RouteStart(state), llm, RouteGate(state, escalator)]
    else:
        stages += [llm]
    stages += [tts, transport.output(), aggregators.assistant()]

    task = PipelineTask(
        Pipeline(stages),
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
        observers=[TurnAwareRTVIObserver(rtvi), latency],
    )

    delivery = (asyncio.create_task(deliver_answers(state, escalator, task))
                if ROUTING_ENABLED else None)

    if os.getenv("VOICE_DEBUG_CONTEXT") == "1":
        @aggregators.assistant().event_handler("on_assistant_turn_stopped")
        async def on_assistant_turn_stopped(agg, *a):
            logger.warning("CONTEXT AFTER TURN:\n" + "\n".join(
                f"  [{m.get('role')}] {str(m.get('content'))[:220]}"
                for m in context.get_messages()))

    @rtvi.event_handler("on_client_ready")
    async def on_client_ready(rtvi):
        await rtvi.set_bot_ready()
        # pipecat 1.7 renamed this; the public getter is gone and the old call
        # threw inside the event handler, which swallowed the opening line.
        await task.queue_frames([aggregators.user()._get_context_frame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Client disconnected, cancelling task")
        await task.cancel()

    try:
        await PipelineRunner(handle_sigint=False).run(task)
    finally:
        if delivery:
            delivery.cancel()
        await state.aclose()


ICE_STUN_URL = "stun:stun.l.google.com:19302"
ice_servers = [IceServer(urls=ICE_STUN_URL)]
request_handler = SmallWebRTCRequestHandler(ice_servers=ice_servers)


async def warm_models():
    """Pay model load once at startup instead of inside the user's first turn."""
    import mlx_whisper
    import numpy as np

    t0 = time.perf_counter()
    await get_worker(voice=TTS_VOICE).ensure_ready()
    logger.info(f"TTS warm in {time.perf_counter() - t0:.1f}s")

    t0 = time.perf_counter()
    silence = np.zeros(16000, dtype=np.float32)
    await asyncio.to_thread(
        mlx_whisper.transcribe, silence, path_or_hf_repo=STT_MODEL, temperature=0.0, language="en"
    )
    logger.info(f"STT warm in {time.perf_counter() - t0:.1f}s")

    if ROUTING_ENABLED:
        # Loads each router model and seats its system prompt in Ollama's prompt
        # cache, so the first real classification is not the one that pays for it.
        # Loading is done per model with a long timeout first: a cold model can
        # take longer than the router's 4 s budget, and a voter that times out
        # counts as a JILL vote, so warming through the normal path would send
        # the first few real turns to Venice for no reason.
        t0 = time.perf_counter()
        import httpx
        async with httpx.AsyncClient() as c:
            for mdl in [ROUTER_MODEL] + ([ROUTER_MODEL_2] if ROUTER_MODEL_2 else []):
                await router_mod.classify_llm("what's the capital of Portugal", c,
                                              model=mdl, timeout=180.0)
            d = await router_mod.route("what's the capital of Portugal", c,
                                       model=ROUTER_MODEL, prefilter=False)
        logger.info(f"Router warm in {time.perf_counter() - t0:.1f}s "
                    f"({ROUTER_MODEL} + {ROUTER_MODEL_2 or 'no second voter'}, "
                    f"probe -> {d.route.value})")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await warm_models()
    logger.info("Ready")
    yield
    await request_handler.close()
    get_worker().shutdown()


app = FastAPI(lifespan=lifespan)


@app.get("/healthz")
async def healthz():
    return {"ok": True, "llm_model": LLM_MODEL, "stt_model": STT_MODEL,
            "router_model": ROUTER_MODEL if ROUTING_ENABLED else None,
            "router_model_2": ROUTER_MODEL_2 if ROUTING_ENABLED else None,
            "confirm_jax": confirm.CONFIRM_ENABLED and ROUTING_ENABLED,
            "escalation_dry_run": escalation.DRY_RUN}


@app.get("/status")
async def status():
    """Transports this server speaks. The client probes this before connecting."""
    return {"status": "ready", "transports": ["webrtc"]}


@app.post("/start")
async def start_session(request: Request):
    """RTVI session handshake, which the client does BEFORE sending its offer.

    This is the request that shows up as "authenticating" in the client. We hold
    no server-side session state - the WebRTC offer that follows is what actually
    starts the bot - so this only has to mint an id and hand back ICE config when
    asked. Pipecat's own runner does the same for the webrtc transport.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}

    result: dict = {"sessionId": str(uuid.uuid4())}
    if body.get("enableDefaultIceServers"):
        result["iceConfig"] = {"iceServers": [{"urls": [ICE_STUN_URL]}]}
    return result


@app.post("/api/offer")
async def offer(request: dict, background_tasks: BackgroundTasks):
    async def start(connection: SmallWebRTCConnection):
        background_tasks.add_task(run_bot, connection)

    return await request_handler.handle_web_request(
        SmallWebRTCRequest.from_dict(request), start
    )


# Pipecat Cloud scopes signaling under /sessions/<id>/. The prebuilt client will
# use that shape when /start hands it a sessionId, so accept both spellings and
# ignore the id - this server only ever runs one conversation at a time.
@app.post("/sessions/{session_id}/api/offer")
async def offer_scoped(session_id: str, request: dict, background_tasks: BackgroundTasks):
    return await offer(request, background_tasks)


@app.patch("/api/offer")
async def ice_candidate(request: SmallWebRTCPatchRequest):
    """Trickle ICE candidates for an in-flight connection.

    Without this the client's first PATCH hits the static-file mount below and
    gets a 405, which the RTVI client treats as fatal - so the connection dies
    at "authenticating" with "Method Not Allowed" and never negotiates. Pipecat's
    own runner registers this alongside the POST; the spike only had the POST.
    """
    await request_handler.handle_patch_request(request)
    return {"status": "success"}


@app.patch("/sessions/{session_id}/api/offer")
async def ice_candidate_scoped(session_id: str, request: SmallWebRTCPatchRequest):
    return await ice_candidate(request)


app.mount("/", SmallWebRTCPrebuiltUI)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Jax local voice agent")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=7860)
    args = parser.parse_args()

    logger.info(f"LLM {LLM_MODEL} via {LLM_BASE_URL} | STT {STT_MODEL} | TTS Kokoro/{TTS_VOICE} "
                f"| router {ROUTER_MODEL if ROUTING_ENABLED else 'OFF'}"
                f"{'+' + ROUTER_MODEL_2 if ROUTING_ENABLED and ROUTER_MODEL_2 else ''} "
                f"| confirm Jax {'ON' if confirm.CONFIRM_ENABLED else 'OFF'}"
                f"{' | ESCALATION DRY RUN' if escalation.DRY_RUN else ''}")
    uvicorn.run(app, host=args.host, port=args.port)
