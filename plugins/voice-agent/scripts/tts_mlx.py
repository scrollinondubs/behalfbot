"""Process-isolated Kokoro TTS service for Pipecat.

Two things matter here:

1. The model runs in a subprocess. mlx-audio grabs a Metal command queue and
   sharing it with MLX Whisper in the parent process deadlocks on Apple Silicon.

2. The subprocess is a module-level singleton, warmed at server startup. Pipecat
   builds a fresh service per WebRTC connection, so a per-service worker would
   pay model load plus warmup (~3s) inside the user's first turn.

Adapted from kwindla/macos-local-voice-agents (server/tts_mlx_isolated.py) for
the Pipecat 1.7 TTSService API.
"""

import asyncio
import base64
import json
import select
import subprocess
import sys
from pathlib import Path
from typing import AsyncGenerator

from loguru import logger
from pipecat.frames.frames import (
    ErrorFrame,
    Frame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
)
from pipecat.services.tts_service import TTSService

WORKER_SCRIPT = str(Path(__file__).parent / "kokoro_worker.py")

DEFAULT_MODEL = "mlx-community/Kokoro-82M-bf16"
DEFAULT_VOICE = "af_heart"

# Model load plus the warmup generate, on a cold HF cache.
INIT_TIMEOUT_SECS = 180.0
GENERATE_TIMEOUT_SECS = 60.0


class KokoroWorker:
    """Owns the Kokoro subprocess. One instance per server."""

    def __init__(self, model: str, voice: str):
        self._model = model
        self._voice = voice
        self._process: subprocess.Popen | None = None
        self._ready = False
        self._lock = asyncio.Lock()

    def _spawn(self):
        self._process = subprocess.Popen(
            [sys.executable, WORKER_SCRIPT],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=0,
        )
        logger.info(f"Kokoro worker started (pid {self._process.pid})")

    def _command(self, payload: dict, timeout: float) -> dict:
        try:
            if not self._process or self._process.poll() is not None:
                self._spawn()
                self._ready = False

            self._process.stdin.write(json.dumps(payload) + "\n")
            self._process.stdin.flush()

            ready, _, _ = select.select([self._process.stdout], [], [], timeout)
            if not ready:
                return {"error": f"Kokoro worker timed out after {timeout}s"}

            line = self._process.stdout.readline()
            if not line:
                return {"error": "Kokoro worker closed stdout"}
            return json.loads(line.strip())
        except Exception as e:
            return {"error": str(e)}

    async def _call(self, payload: dict, timeout: float) -> dict:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._command, payload, timeout)

    async def ensure_ready(self):
        async with self._lock:
            if self._ready:
                return
            result = await self._call(
                {"cmd": "init", "model": self._model, "voice": self._voice},
                INIT_TIMEOUT_SECS,
            )
            if not result.get("success"):
                raise RuntimeError(f"Kokoro init failed: {result.get('error')}")
            self._ready = True
            logger.info(f"Kokoro worker ready ({self._model}, voice={self._voice})")

    async def generate(self, text: str) -> bytes:
        await self.ensure_ready()
        async with self._lock:
            result = await self._call({"cmd": "generate", "text": text}, GENERATE_TIMEOUT_SECS)
        if not result.get("success"):
            raise RuntimeError(f"Kokoro generation failed: {result.get('error')}")
        return base64.b64decode(result["audio"])

    def shutdown(self):
        if self._process:
            try:
                self._process.terminate()
                self._process.wait(timeout=5)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass
            self._process = None
            self._ready = False


_worker: KokoroWorker | None = None


def get_worker(model: str = DEFAULT_MODEL, voice: str = DEFAULT_VOICE) -> KokoroWorker:
    global _worker
    if _worker is None:
        _worker = KokoroWorker(model, voice)
    return _worker


class KokoroMLXTTSService(TTSService):
    """Kokoro TTS via mlx-audio, backed by the shared worker process."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        voice: str = DEFAULT_VOICE,
        sample_rate: int = 24000,
        **kwargs,
    ):
        super().__init__(sample_rate=sample_rate, **kwargs)
        self._worker = get_worker(model, voice)

    def can_generate_metrics(self) -> bool:
        return True

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        try:
            await self.start_ttfb_metrics()
            await self.start_tts_usage_metrics(text)
            yield TTSStartedFrame()

            audio_bytes = await self._worker.generate(text)
            await self.stop_ttfb_metrics()

            chunk_size = self.chunk_size
            for i in range(0, len(audio_bytes), chunk_size):
                chunk = audio_bytes[i : i + chunk_size]
                if chunk:
                    yield TTSAudioRawFrame(chunk, self.sample_rate, 1)
                    await asyncio.sleep(0.001)
        except Exception as e:
            logger.error(f"TTS error: {e}")
            yield ErrorFrame(error=str(e))
        finally:
            await self.stop_ttfb_metrics()
            yield TTSStoppedFrame()
