# Laya (System One decision model) on Cloudflare Containers - behalfbot#214,
# part of the FounderOS epic (behalfbot-plugins#22).
#
# CPU laya-serve with BOTH checkpoints baked into the image at build time
# (each running instance holds one of them in memory, see serve.py):
#   english      - the one to use for English pain tagging (AUC 0.78 on the
#                  new-jaxity#603 eval; multilingual was at chance there)
#   multilingual - kept for non-English founders and the shadow comparison
#
# A cold start therefore never touches Hugging Face. HF_HUB_OFFLINE=1 makes
# that a hard guarantee rather than a cache hit: if a file were missing the
# load fails loudly instead of quietly downloading 1+ GB on a 1/2 vCPU box.
# The Worker also runs this container with enableInternet = false.
#
# Same base, torch pin and laya pin as new-jaxity docker/laya/Dockerfile, so
# self-hosted and SaaS answer identically. Bump both together.
#
# Cloudflare builds and runs linux/amd64. Local check:
#   docker build --platform linux/amd64 -t behalfbot-laya-cf .
FROM python:3.12-slim

ENV PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/models/hf

# CPU wheel index first so pip never pulls a CUDA build of torch.
RUN pip install --index-url https://download.pytorch.org/whl/cpu "torch==2.14.*" \
 && pip install "laya[serve]==0.3.20"

RUN useradd --create-home --uid 10001 laya \
 && mkdir -p /models/hf \
 && chown -R laya:laya /models
USER laya

# Bake the weights. Building each Agent (not just snapshot_download) also runs
# laya's one-time tokenizer_config patch, so the runtime load is read-only.
RUN python3 -c "from laya.router import Router; Router(device='cpu').preload(['english', 'multilingual'])" \
 && python3 -c "import shutil; shutil.rmtree('/models/hf/xet', ignore_errors=True)"

# One thread everywhere. standard-1 is 1/2 vCPU, but torch and OpenMP size
# their pools from the host core count, and idle OpenMP workers spin-wait.
# Under a 0.5 CPU quota that spinning burns the quota: measured locally with
# --cpus=0.5, LAYA_THREADS=1 alone gave 20.4 s per english call; adding
# OMP_NUM_THREADS=1 and MKL_NUM_THREADS=1 brought it to 8.7 s.
# LAYA_PORT=8080 matches the executor and support-agent containers.
ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    OMP_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    LAYA_HOST=0.0.0.0 \
    LAYA_PORT=8080 \
    LAYA_DEVICE=cpu \
    LAYA_THREADS=1 \
    LAYA_MODEL=english

# Both checkpoints are on disk; each instance holds one, picked by LAYA_MODEL
# (the Worker sets it per instance). serve.py has the measurements behind that.
COPY --chown=laya:laya serve.py /app/serve.py

EXPOSE 8080

CMD ["python3", "/app/serve.py"]
