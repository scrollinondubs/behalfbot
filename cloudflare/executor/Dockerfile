# Behalf.bot executor container - issues #41 / #66.
#
# Runs the queue executor (clone member repo + claude -p + parse + PR)
# inside a Cloudflare Container. One tick per HTTP trigger, scale-to-zero
# between runs.
#
# TRUST BOUNDARY (issue #66) - enforced by what this image simply does not
# contain:
#   - No chassis skills, no dating context, no ~/jax-private, no SiYuan,
#     no Discord-bridge identity, no Vaultwarden client, no OAuth tokens.
#   - Only the executor slice of the vibecodelisboa repo (prepared by
#     build.sh into build-context/, secrets and git history stripped).
#   - Runtime secrets are limited to the five listed in the README. The
#     image itself bakes NO secret of any kind.
#
# Build: run ./build.sh first (populates build-context/), then let
# wrangler build this Dockerfile on deploy. Never `docker build` with the
# raw vibecodelisboa checkout as context.

FROM node:22-bookworm-slim

# git for member-repo clones; ca-certificates for HTTPS egress to
# github.com / api.anthropic.com / Turso.
RUN apt-get update \
  && apt-get install -y --no-install-recommends git ca-certificates \
  && rm -rf /var/lib/apt/lists/*

# The claude CLI the processor spawns (claude -p --bare ...). Pinned so
# image rebuilds are reproducible; bump deliberately.
RUN npm install -g @anthropic-ai/claude-code

# Non-root runtime user. The executor needs no privileges beyond its own
# workdir; container escape hardening starts with not being root.
RUN useradd --create-home --shell /usr/sbin/nologin executor

# Executor code slice, prepared by build.sh:
#   build-context/vibecodelisboa/  - shallow clone, .git removed, .env*
#                                    removed, NO node_modules (installed
#                                    below so platform binaries match the
#                                    linux image, not the macOS build host)
#
# Manifests first so the dependency layer caches across source-only
# rebuilds. npm install (not ci) because the repo lockfile is currently
# out of sync with package.json (missing platform optionals); dev deps
# included on purpose - tsx is a devDependency and the shim spawns it.
COPY build-context/vibecodelisboa/package.json build-context/vibecodelisboa/package-lock.json /app/vibecodelisboa/
# HUSKY=0: the repo's prepare script runs husky, which fails in a context
# with no .git (build.sh strips it).
RUN cd /app/vibecodelisboa \
  && HUSKY=0 npm install --no-audit --no-fund \
  && npm cache clean --force

COPY --chown=executor:executor build-context/vibecodelisboa /app/vibecodelisboa

# In-container HTTP shim (trigger endpoint the Worker forwards to).
#
# Copy the whole directory, not the single entrypoint file. server.mjs imports
# ./prescan-outcome.mjs, which arrived with the pre-scan chaining work (#166)
# while this line still copied exactly one file - so the first image built from
# that code could not boot at all:
#
#   Error [ERR_MODULE_NOT_FOUND]: Cannot find module '/app/prescan-outcome.mjs'
#   imported from /app/server.mjs
#
# What made it expensive to find: the running instance stayed warm across the
# deploy and kept serving the OLD image, so the deploy looked clean and three
# subsequent runs succeeded. The break only surfaced on the next genuine cold
# start - hours later, triggered by an unrelated secret update, with no obvious
# link back to the deploy that caused it. Every restart after that failed, and
# the platform reported the instance as `running` while the Worker could not
# reach it.
#
# Copying the directory means the next module server.mjs imports ships with it
# rather than breaking the image on first cold start. The .test.mjs file riding
# along is harmless and keeps the rule simple.
COPY --chown=executor:executor container/ /app/

USER executor
WORKDIR /app

# Per-job scratch clones land here; wiped by the processor on success and
# failure, and the whole filesystem is ephemeral anyway.
ENV BEHALFBOT_WORK_ROOT=/tmp/behalfbot-work
ENV BEHALFBOT_LOG_DIR=/tmp/behalfbot-logs
ENV BEHALFBOT_CLAUDE_BIN=claude

EXPOSE 8080

CMD ["node", "/app/server.mjs"]
