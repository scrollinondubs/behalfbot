# Behalf.bot support-agent container - issue #66.
#
# Answers VCL course tech-support questions and guides managed installs.
# One claude -p invocation per /ask, scale-to-zero between asks.
#
# TRUST BOUNDARY (issue #66) - enforced by what this image simply does
# not contain:
#   - Knowledge corpus is ONLY what knowledge-manifest.txt allowlists
#     (public behalfbot repo docs + chassis), staged by build.sh with a
#     fail-closed verifier. No dating skills, no dating context, no
#     ~/jax-private, no company-confidential material, no memory graphs,
#     no SiYuan content.
#   - No git, no PAT, no repo-write path of any kind. The agent reads
#     docs and answers; it cannot touch code or repos.
#   - The image bakes NO secret. Runtime secret set is exactly one:
#     BEHALFBOT_ANTHROPIC_API_KEY. Nothing in this container can reach
#     Vaultwarden, Postgres, SiYuan, the Discord bridge, or any OAuth
#     token of Sean's, because no credential for those systems exists
#     anywhere in the Worker or the container.
#
# Build: run ./build.sh first (stages + verifies build-context/knowledge),
# then let wrangler build this Dockerfile on deploy.

FROM node:22-bookworm-slim

# ca-certificates for HTTPS egress to api.anthropic.com and WebFetch
# lookups. Deliberately NO git: this agent has no repo work.
RUN apt-get update \
  && apt-get install -y --no-install-recommends ca-certificates \
  && rm -rf /var/lib/apt/lists/*

# The claude CLI the shim spawns (claude -p --bare ...).
RUN npm install -g @anthropic-ai/claude-code

# Non-root runtime user.
RUN useradd --create-home --shell /usr/sbin/nologin support

# Knowledge corpus (staged + boundary-verified by build.sh) and persona.
# Owned by root, world-readable: the "support" user can read but never
# modify its own corpus or prompt.
COPY build-context/knowledge /app/knowledge
COPY persona/SUPPORT_AGENT.md /app/persona/SUPPORT_AGENT.md

# In-container HTTP shim (/ask endpoint the Worker forwards to).
COPY --chown=support:support container/server.mjs /app/server.mjs

USER support
WORKDIR /app

EXPOSE 8080

CMD ["node", "/app/server.mjs"]
