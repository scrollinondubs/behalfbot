#!/usr/bin/env python3
"""Standalone Kokoro TTS worker.

Runs in its own process and speaks JSON over stdin/stdout. The isolation is not
cosmetic: mlx-audio grabs a Metal command queue, and sharing that with the MLX
Whisper model in the parent process deadlocks on Apple Silicon.

Commands:
    {"cmd": "init", "model": "mlx-community/Kokoro-82M-bf16", "voice": "af_heart"}
    {"cmd": "generate", "text": "Hello world"}
"""

import base64
import json
import os
import sys
import traceback

import numpy as np

# mlx_audio prints progress ("Creating new KokoroPipeline...") straight to
# stdout, which corrupts the JSON protocol and makes the parent's readline()
# blow up on the first generate. Claim a private copy of the real stdout for
# protocol frames, then point sys.stdout at stderr so library chatter is
# harmless. Must happen before importing mlx_audio.
_protocol_fd = os.dup(1)
os.dup2(2, 1)
PROTOCOL = os.fdopen(_protocol_fd, "w")
sys.stdout = sys.stderr

try:
    from mlx_audio.tts.utils import load_model

    MLX_AVAILABLE = True
except ImportError:
    MLX_AVAILABLE = False


def trim_lead_in(audio, sample_rate=24000, threshold=0.005, keep_ms=20):
    """Drop Kokoro's leading silence.

    Kokoro prepends 300-420 ms of near-silence to every utterance. Sent as-is
    that is dead air the user waits through, and it dominated the measured
    voice-to-voice latency. A short pad is kept so the first phoneme is intact.
    """
    window = int(0.01 * sample_rate)
    for i in range(0, len(audio) - window, window):
        if np.sqrt(np.mean(audio[i : i + window].astype(np.float32) ** 2)) > threshold:
            return audio[max(0, i - int(keep_ms / 1000 * sample_rate)) :]
    return audio


class Worker:
    def __init__(self):
        self.model = None
        self.voice = None

    def initialize(self, model_name, voice):
        if not MLX_AVAILABLE:
            return {"error": "mlx_audio not available"}
        try:
            self.model = load_model(model_name)
            self.voice = voice
            # Warm the graph so the first real turn does not pay compile cost.
            list(self.model.generate(text="test", voice=voice, speed=1.0))
            return {"success": True}
        except Exception as e:
            return {"error": f"{e}\n{traceback.format_exc()}"}

    def generate(self, text):
        try:
            if not self.model:
                return {"error": "Not initialized"}

            segments = [
                np.array(result.audio, copy=True)
                for result in self.model.generate(text=text, voice=self.voice, speed=1.0)
            ]
            if not segments:
                return {"error": "No audio"}

            audio = segments[0] if len(segments) == 1 else np.concatenate(segments, axis=0)
            if np.max(np.abs(audio)) < 1e-6:
                return {"error": "Generated audio is silent"}

            audio = trim_lead_in(audio)

            audio_int16 = (audio * 32767).astype(np.int16)
            return {"success": True, "audio": base64.b64encode(audio_int16.tobytes()).decode()}
        except Exception as e:
            return {"error": f"{e}\n{traceback.format_exc()}"}


def main():
    worker = Worker()
    for line in sys.stdin:
        try:
            req = json.loads(line.strip())
            if req["cmd"] == "init":
                resp = worker.initialize(req["model"], req["voice"])
            elif req["cmd"] == "generate":
                resp = worker.generate(req["text"])
            else:
                resp = {"error": f"Unknown command: {req['cmd']}"}
        except Exception as e:
            resp = {"error": str(e)}
        PROTOCOL.write(json.dumps(resp) + "\n")
        PROTOCOL.flush()


if __name__ == "__main__":
    main()
