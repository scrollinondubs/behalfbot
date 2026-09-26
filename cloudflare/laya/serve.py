# laya-serve pinned to ONE checkpoint per container, for standard-1 (4 GiB).
#
# Both checkpoints are baked into the image, but each container instance holds
# exactly one in memory, chosen at start by LAYA_MODEL (english or
# multilingual). The Worker runs one instance per checkpoint and routes each
# request by its `model` field. Measured with a 4 GiB limit (README.md):
#
#   - both resident, stock laya-serve: 3.7 GiB idle, and one multilingual
#     request (12k-char state, 4 questions) was OOM-killed
#   - swapping in-process (Router max_loaded=1) does not give the memory back:
#     multilingual alone is 1.6 GiB, but after one english round trip it sat
#     at 3.2 GiB, and it crept up on every swap
#   - one pinned checkpoint: english peaked at 2.8 GiB on 16 questions,
#     multilingual at 3.8 GiB (see the question cap in src/index.ts)
#
# Pinning also means a request with no `model`, which stock laya would
# auto-route by language, can never pull the other checkpoint into memory.
import os

import torch
import uvicorn
from laya.router import Router
from laya.serve import create_app

MODEL = os.environ.get("LAYA_MODEL", "english")
if MODEL not in ("english", "multilingual"):
    raise SystemExit("LAYA_MODEL must be english or multilingual, got %r" % MODEL)


class PinnedRouter(Router):
    def predict(self, state, questions, model=None, **kwargs):
        return super().predict(state, questions, model=MODEL, **kwargs)


torch.set_num_threads(int(os.environ.get("LAYA_THREADS", "1")))

router = PinnedRouter(device=os.environ.get("LAYA_DEVICE") or "cpu", max_loaded=1)
router.preload([MODEL])

uvicorn.run(
    create_app(router),
    host=os.environ.get("LAYA_HOST", "0.0.0.0"),
    port=int(os.environ.get("LAYA_PORT", "8080")),
    log_level=os.environ.get("LAYA_LOG_LEVEL", "info"),
)
