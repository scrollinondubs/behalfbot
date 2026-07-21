# syntax=docker/dockerfile:1.7
#
# Behalf.bot chassis image
# ========================
# Published to ghcr.io/scrollinondubs/behalfbot-chassis:{latest,vX.Y.Z,<sha>}
#
# What's inside (image-baked):
#   - Debian bookworm base, Python 3.12, Node 22 LTS, bun, uv, claude CLI, rbw
#   - ffmpeg, sqlite3, jq, curl, git, zsh, tini, build-essential
#   - chassis source tree at /app/chassis (read-only at runtime)
#   - plugins source tree at /app/plugins (read-only at runtime)
#   - entrypoint dispatcher loop at /app/docker/entrypoint.sh
#
# What's NOT inside (volume-mounted per-customer):
#   - .env, .mcp.json, chassis.config.yaml, INSTALL_PROFILE.md, CLAUDE.md
#   - data/, state/, briefings/, logs/, memory/
#   - ~/.claude/.credentials.json (OAuth)
#
# Volume contract: host's customer dir bind-mounts to /app/customer.
# See docs/containerization.md for the full mount layout.

ARG PYTHON_VERSION=3.12
ARG NODE_MAJOR=22
ARG BUN_VERSION=1.1.34
ARG RBW_VERSION=1.13.2

# ---------- stage 1: rbw builder (cross-compiles from native host, no QEMU) --
# rbw is the Rust Bitwarden/Vaultwarden CLI. We build from source because
# Debian 12 doesn't ship rbw. V1 install lesson: bw (the official npm CLI)
# enforces HTTPS and chokes on self-hosted VW without proper TLS. rbw works
# fine over HTTP. Builder stage keeps Rust toolchain out of the runtime image.
#
# --platform=$BUILDPLATFORM pins this stage to the native runner arch so
# cargo never runs under QEMU emulation. For arm64 targets we cross-compile
# via gcc-aarch64-linux-gnu + multiarch libssl-dev:arm64. The cargo build
# runs at native speed (~5 min) instead of the 40+ min QEMU timeout that
# caused arm64 to be dropped in #35.

FROM --platform=$BUILDPLATFORM rust:1.83-slim-bookworm AS rbw-builder
ARG TARGETARCH
ARG RBW_VERSION
RUN dpkg --add-architecture arm64 \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates pkg-config \
        gcc-aarch64-linux-gnu \
        libc6-dev-arm64-cross \
        libssl-dev libssl-dev:arm64 \
 && rm -rf /var/lib/apt/lists/*
RUN set -eux; \
    if [ "$TARGETARCH" = "arm64" ]; then \
        rustup target add aarch64-unknown-linux-gnu; \
        CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER=aarch64-linux-gnu-gcc \
        PKG_CONFIG_ALLOW_CROSS=1 \
        PKG_CONFIG_LIBDIR=/usr/lib/aarch64-linux-gnu/pkgconfig \
        cargo install --locked --version "$RBW_VERSION" rbw \
            --target aarch64-unknown-linux-gnu --root /rbw-out; \
        aarch64-linux-gnu-strip /rbw-out/bin/rbw; \
    else \
        cargo install --locked --version "$RBW_VERSION" rbw --root /rbw-out; \
        strip /rbw-out/bin/rbw; \
    fi

# ---------- stage 2: runtime image -------------------------------------------

FROM python:${PYTHON_VERSION}-slim-bookworm

ARG NODE_MAJOR
ARG BUN_VERSION
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CHASSIS_ROOT=/app/chassis \
    CUSTOMER_HOME=/app/customer \
    CHASSIS_HOME=/app/customer \
    HOME=/home/chassis \
    PATH=/home/chassis/.local/bin:/home/chassis/.bun/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# CHASSIS_PLUGINS_ROOT is deliberately NOT baked as ENV. The entrypoint
# resolves it at boot via chassis/scripts/resolve-plugin-root.sh, overlaying
# $CUSTOMER_HOME/vendored-plugins over /app/plugins per plugin name. A baked
# ENV value is indistinguishable from an operator override and is exactly what
# made the v0.2.0 fetched-tree preference in _env.sh unreachable.

# System dependencies. Note: NO bw CLI; rbw is the Vaultwarden client (HTTPS
# enforcement bypass per V1 install lesson). NO cron daemon; entrypoint
# runs the dispatcher in a sleep-loop instead - fewer attack surfaces, same
# 15-min cadence.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        gnupg \
        jq \
        ffmpeg \
        passwd \
        sqlite3 \
        zsh \
        tini \
        pinentry-tty \
        libssl3 \
        unzip \
        awscli \
        age \
        openssh-client \
 && curl -fsSL https://deb.nodesource.com/setup_${NODE_MAJOR}.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

# GitHub CLI (gh) - not in Debian default repos so install via the official
# GitHub Apt repository. Required by any heartbeat that triages issues,
# manages PRs, or fans out a workflow from inside the container. V1 install
# gather scripts (gather-new-issues.sh, etc.) hit `gh: command not found`
# until this layer landed.
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
 && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends gh \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

# PostgreSQL client (pg_dump). Required by the `pg-backup.sh` heartbeat to
# dump the chassis postgres container nightly. Debian bookworm's default
# `postgresql-client` package is v15; chassis installs run pg17 (pgvector
# is on pg17), and pg_dump requires server-version <= client-version.
# Use postgresql.org's apt repo to pin client to 17.
RUN curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        | gpg --dearmor -o /etc/apt/trusted.gpg.d/postgresql.gpg \
 && echo "deb http://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client-17 \
 && update-alternatives --install /usr/bin/pg_dump pg_dump /usr/lib/postgresql/17/bin/pg_dump 200 \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

# rmapi (ddvk fork) — reMarkable cloud sync CLI. Required by the
# remarkable-health heartbeat AND the canonical OTA send / notebook-rw
# scripts under plugins/remarkable/scripts/. The legacy juruen fork is
# dormant since the reMarkable API shifted; ddvk tracks the live API
# (rotates with the cloud's auth schema). Pin to a release tag rather
# than `latest` so a future upstream change doesn't silently break the
# image build.
#
# rmapi releases ship linux-amd64 and linux-arm64 separately. The
# multi-arch chassis build picks the right asset via $TARGETARCH (set
# automatically by buildx for each target platform).
ARG RMAPI_VERSION=v0.0.34
ARG TARGETARCH
RUN set -eux; \
    case "${TARGETARCH}" in \
        amd64) rmapi_asset=rmapi-linux-amd64.tar.gz ;; \
        arm64) rmapi_asset=rmapi-linux-arm64.tar.gz ;; \
        *)     echo "Unsupported TARGETARCH=${TARGETARCH} for rmapi install" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/rmapi.tgz \
        "https://github.com/ddvk/rmapi/releases/download/${RMAPI_VERSION}/${rmapi_asset}"; \
    tar -xzf /tmp/rmapi.tgz -C /tmp; \
    install -m 0755 /tmp/rmapi /usr/local/bin/rmapi; \
    rm -f /tmp/rmapi /tmp/rmapi.tgz; \
    /usr/local/bin/rmapi version

# turso CLI — Required by the `turso-backup` heartbeat to nightly-dump every
# Turso database in the install's allowlist (default: vibecodelisboa,
# vibecodelisboa-preview, etc). Without this, the heartbeat correctly
# fires `count=1 issues=["turso CLI not in PATH"]` per its post-cutover
# loud-fail contract, surfaces "Turso DBs are NOT being backed up" in
# briefings, and accrues backup gap days until somebody installs it.
#
# Auth state lives at $HOME/.config/turso/settings.json (host-side); the
# customer .env / Vaultwarden flow re-hydrates it on container start.
#
# turso-cli releases ship Linux x86_64 and arm64 asset tarballs (binary
# at root, shell completions in completions/). Pin to a release tag so
# the image build is reproducible.
ARG TURSO_VERSION=v1.0.26
RUN set -eux; \
    case "${TARGETARCH}" in \
        amd64) turso_asset=turso-cli_Linux_x86_64.tar.gz ;; \
        arm64) turso_asset=turso-cli_Linux_arm64.tar.gz ;; \
        *)     echo "Unsupported TARGETARCH=${TARGETARCH} for turso install" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/turso.tgz \
        "https://github.com/tursodatabase/turso-cli/releases/download/${TURSO_VERSION}/${turso_asset}"; \
    tar -xzf /tmp/turso.tgz -C /tmp; \
    install -m 0755 /tmp/turso /usr/local/bin/turso; \
    rm -rf /tmp/turso /tmp/completions /tmp/turso.tgz; \
    /usr/local/bin/turso --version

# rmscene + rmc — Python tools for parsing/composing reMarkable .rm files.
# Used by the notebook-rw.sh script (read renders pages → SVG → PNG; write
# composes typed-text pages and re-zips the bundle). Installed system-wide
# so plugin scripts don't need per-user venvs. ImageMagick provides the
# SVG → PNG raster step.
RUN apt-get update \
 && apt-get install -y --no-install-recommends imagemagick \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir 'rmscene>=0.6,<1' 'rmc>=0.3,<1'

# Docker CLI — required by the `docker-prune` heartbeat to clean stale
# build cache + unused images from the host docker daemon. The chassis
# container itself doesn't run docker-in-docker; the CLI talks to the
# host daemon over the bind-mounted socket (configured in compose).
#
# Security model: mounting /var/run/docker.sock effectively grants the
# container root on the host via container escape. Mitigations:
#   - The chassis container is single-tenant (one installer, one process
#     tree), so the blast radius is the installer's own machine.
#   - docker-prune-only operations don't require pull / push / run, but
#     the socket exposes those capabilities anyway. A future hardening
#     pass should swap the raw socket for tecnativa/docker-socket-proxy
#     restricted to `builder.prune` + `images` endpoints.
#
# Install from Docker's apt repo so the CLI version stays in sync with
# Docker Desktop / Docker Engine 27+. Only the `docker-ce-cli` package
# is needed — no daemon, no compose, no buildx (we're a client only).
RUN install -m 0755 -d /etc/apt/keyrings \
 && curl -fsSL https://download.docker.com/linux/debian/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
 && chmod a+r /etc/apt/keyrings/docker.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian bookworm stable" \
        > /etc/apt/sources.list.d/docker.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends docker-ce-cli \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

# Python runtime deps baked into the image. Gather scripts + plugin runtimes
# + dispatcher Python helpers all assume these are importable. Installed
# system-wide (as root, before dropping to chassis user) so every shell
# inside the container resolves them without per-user venv setup. See
# requirements.txt for inventory + rationale; bump that file in a discrete
# PR when an upstream module ships a breaking change worth absorbing.
COPY requirements.txt /tmp/chassis-requirements.txt
RUN pip install --no-cache-dir -r /tmp/chassis-requirements.txt \
 && rm -f /tmp/chassis-requirements.txt

# Non-root runtime user. UID/GID 1000 by default; override at build time
# (--build-arg INSTALLER_UID=...) if the customer's host UID differs and
# bind-mount permissions matter.
ARG INSTALLER_UID=1000
ARG INSTALLER_GID=1000
# Handle GID collision with Debian base groups. On macOS hosts, the user's
# primary GID is typically 20 (`staff`), which collides with Debian's
# pre-existing `dialout` group at GID 20 — `groupadd --gid 20 chassis`
# then fails with "GID '20' already exists" and the build aborts. The
# conditional below renames the existing group to `chassis` instead of
# creating a new one when the GID is already taken, so `useradd --gid
# chassis` succeeds in both cases (fresh GID → groupadd path; reused GID
# → groupmod path).
RUN if getent group ${INSTALLER_GID} >/dev/null; then \
        groupmod -n chassis "$(getent group ${INSTALLER_GID} | cut -d: -f1)"; \
    else \
        groupadd --gid ${INSTALLER_GID} chassis; \
    fi \
 && useradd  --uid ${INSTALLER_UID} --gid chassis --shell /usr/bin/zsh --create-home chassis

# macOS-installer passwd entry. The CI build sets INSTALLER_UID=1000 (Linux
# convention), so the GHCR-published image's /etc/passwd has `chassis:1000`
# but no entry for the macOS standard first-user UID 501. When a macOS host
# runs the container as `--user 501:20` (set in the compose yaml for
# bind-mount file-ownership alignment), `id` and any getpwuid/getgrgid call
# fails with "no such user" — entrypoint dies on the first such call,
# container loops on startup. (<v1-reference-install>#700 P0 trace 2026-05-25.)
#
# Two-pronged fix without rebuilding per-installer:
# 1. Always create an `installer` account at UID 501 / GID 20. macOS's
#    first-user UID is 501 and primary GID is 20 (`staff`) on every modern
#    macOS host — covers 100% of macOS installs without overrides.
# 2. Linux installs that run at UID 1000 still hit the chassis user; Linux
#    installs at other UIDs continue to need a per-install build with
#    --build-arg INSTALLER_UID=$(id -u).
#
# Both users share /home/chassis so bind-mounted state is reachable by
# whichever account the runtime ends up being. Skipped when the build-time
# INSTALLER_UID is already 501 (don't double-claim the slot).
ARG MACOS_INSTALLER_UID=501
ARG MACOS_INSTALLER_GID=20
RUN if [ "${INSTALLER_UID}" != "${MACOS_INSTALLER_UID}" ]; then \
        if ! getent group ${MACOS_INSTALLER_GID} >/dev/null; then \
            groupadd --gid ${MACOS_INSTALLER_GID} installer; \
        fi; \
        useradd --uid ${MACOS_INSTALLER_UID} --gid ${MACOS_INSTALLER_GID} \
            --shell /bin/bash --no-create-home --home-dir /home/chassis installer; \
    fi

# Bring in rbw from the builder stage.
COPY --from=rbw-builder /rbw-out/bin/rbw /usr/local/bin/rbw

# Drop privileges for all subsequent tool installs so npm/bun caches land
# in chassis user's home.
USER chassis
WORKDIR /home/chassis

# bun (hard prereq for `claude --channels` per V1 install lesson)
RUN curl -fsSL https://bun.sh/install | bash -s "bun-v${BUN_VERSION}"

# uv (Python tool runner - the chassis bootstrap + plugin install scripts
# call `uv` and `uvx` directly).
RUN curl -fsSL https://astral.sh/uv/install.sh | sh

# Claude Code CLI. Install via npm so it lands in /home/chassis/.local/bin
# (npm prefix configured below). Pinned to a known-good major; floating
# inside major is acceptable for a CLI.
RUN mkdir -p /home/chassis/.local \
 && npm config set prefix /home/chassis/.local \
 && npm install -g @anthropic-ai/claude-code@latest

# Application code. Chassis source, plugins, docs, scripts - image-baked,
# read-only at runtime. Customer-specific state lives in /app/customer
# (bind-mounted from host).
USER root
RUN mkdir -p /app/chassis /app/plugins /app/docker /app/customer \
 && chown -R chassis:chassis /app
COPY --chown=chassis:chassis chassis/   /app/chassis/
COPY --chown=chassis:chassis plugins/   /app/plugins/
COPY --chown=chassis:chassis docker/    /app/docker/
COPY --chown=chassis:chassis bootstrap.sh chassis.config.yaml INSTALL_PROFILE.md README.md /app/

# Make /home/chassis writable by any UID the compose runtime drops to.
#
# Why this exists:
# This image is published from CI with INSTALLER_UID=1000 baked in, so the
# `chassis` user inside the image is UID 1000 and `/home/chassis` is owned
# `chassis:chassis` at mode 0755. Customer installs that pull the published
# image (rather than rebuilding locally with `--build-arg INSTALLER_UID=$(id -u)`)
# end up running the container as their host UID (e.g. 501 on macOS), which
# does not match the baked chassis user. The entrypoint's bridge cp into
# `/home/chassis/.claude.json` then fails silently with EACCES (the cp is
# wrapped in `2>/dev/null` so the error doesn't even surface in logs) and
# every claude invocation thereafter returns "Not logged in".
#
# Confirmed concretely 2026-05-25 on scrollinondubs/new-jaxity — see
# <v1-reference-install>#698 follow-up. The chassis-design-intent path is for
# every install to rebuild locally with matching UID/GID build args, but
# making the published image work at any UID-without-rebuild is the lower-
# friction default. This chmod keeps the chassis user as owner (so the
# build-time tool installs into .bun/.local/.npm aren't disturbed) but
# adds o+rwx so any UID dropped into the container can read tools + write
# its own state files in $HOME root.
#
# Trade-off: 0777 looks scary in security reviews. Inside a single-tenant
# container with no other shells / users running, the blast radius is the
# same as a 0755 chassis-owned directory — there's no "other user" to
# protect against. Document this in the security model, but the simpler
# customer story wins.
RUN chmod 0777 /home/chassis

# Tool caches need to be writable by any-UID runtime, not just the baked
# chassis user (UID 1000). The `chmod 0777 /home/chassis` above only covers
# the top dir — subdirs (.npm, .bun, .local, .config, .cache) keep their
# install-time perms (chassis:chassis 0755), which EACCESes any runtime UID
# that tries to use npm/bun/cargo/etc.
#
# Concrete trigger 2026-05-25: gather-midnight-oil.sh fires `npx ccusage`,
# npm needs to write its cache at /home/chassis/.npm/_cacache/tmp/<hash>,
# UID 501 (installer) can't write → EACCES → script returns count=0 reason=
# ccusage_failed every tick. Same wall hits any future npm/bun/uv-using
# gather. (<v1-reference-install>#700)
#
# Fix: make every relevant tool-cache dir group/other-writable so any UID
# can populate it. We don't `-R` on /home/chassis to keep `chmod 0777` on
# subdirs that might contain sensitive bind-mounted state (.claude/, .rmapi);
# instead enumerate the tool dirs explicitly.
RUN for d in .npm .bun .local .config .cache; do \
        mkdir -p "/home/chassis/$d"; \
        chmod -R 0777 "/home/chassis/$d" 2>/dev/null || true; \
    done

USER chassis
WORKDIR /app/customer
VOLUME ["/app/customer", "/home/chassis/.claude"]

# Healthcheck: the entrypoint writes a heartbeat sentinel after each
# dispatcher tick. If the sentinel is older than 20 min, the container is
# stuck.
HEALTHCHECK --interval=2m --timeout=10s --start-period=1m --retries=3 \
    CMD test -f /tmp/dispatcher.alive \
        && [ $(( $(date +%s) - $(stat -c %Y /tmp/dispatcher.alive) )) -lt 1200 ] \
        || exit 1

# tini reaps zombies. dispatcher is the default mode; `bootstrap`,
# `install-plugin <name>`, `shell`, and `claude` are documented in entrypoint.
ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker/entrypoint.sh"]
CMD ["dispatcher"]
