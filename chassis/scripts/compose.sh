#!/usr/bin/env bash
# compose.sh - the ONLY supported way to drive `docker compose` against a chassis
# install. It always resolves the right compose files, --env-file, project name and
# CUSTOMER_HOME, and refuses to run when any of them are wrong.
#
# Every guard below exists because of a real outage on a live install. None of them
# are hypothetical. Read the four blocks before deleting one.
#
# --- Guard 1: --env-file=.env.baked (secrets) ---
# Bare `docker compose up` reads `.env` directly. A chassis install's `.env` ends in a
# Vaultwarden hydration block that only resolves at runtime on the HOST via `bw`/`rbw`;
# the literal secrets land in `.env.baked` after bake-env.sh. Compose without
# --env-file=.env.baked silently starts the container with those keys MISSING, and the
# subsystems that need them fall back to defaults instead of failing loudly.
#
# Burn 2026-05-30: a sequence of `docker compose up -d` calls missed --env-file, so the
# container came up without OLLAMA_HOST. The morning briefing's semantic-search step
# silently degraded to its fallback path. Nothing errored. It was caught only because a
# human noticed the briefing had gotten worse.
#
# --- Guard 2: the compose file lives in the CHASSIS repo, not the customer repo ---
# This script's ancestor looked for $CUSTOMER_HOME/chassis/docker-compose.yml. That
# vendored subtree was deliberately DROPPED (behalfbot#136). `chassis/` under a customer
# install is now an empty bind-mount point - the chassis repo is mounted into the
# container at /app/customer/chassis. So the compose file lives in the chassis REPO on
# the host.
#
# Burn: on the reference install the script therefore errored out for six weeks and
# nobody noticed, because the container had been running since 2026-06-02 and was only
# ever `restart`ed, never recreated. That is the real hazard - the container was NOT
# reproducible via the documented path. Had it died, it would not have come back, and
# its env was a six-week-old snapshot. See new-jaxity#280 / #281.
#
# --- Guard 3: pinned project name ---
# Without `-p`, compose derives the project name from the working directory. Run it from
# the wrong directory and `up --force-recreate` cheerfully stands up a SECOND set of
# containers alongside the live ones instead of replacing them. Two dispatchers, one
# bind-mount, no error message.
#
# --- Guard 4: CUSTOMER_HOME must be exported ---
# Compose interpolates CUSTOMER_HOME into the volume paths. It lives in `.env` but is
# deliberately stripped from `.env.baked` by bake-env.sh (it is a HOST path - inside the
# container it is authoritatively /app/customer, and re-sourcing a host path there sends
# every gather script `cd`-ing into a directory that does not exist). So --env-file
# cannot supply it and it must be exported here. Without the export, even
# `docker compose config` fails outright: "required variable CUSTOMER_HOME is missing a
# value".
#
# --- Guard 5: stale bake ---
# If `.env` is newer than `.env.baked`, someone edited `.env` and never re-baked. Coming
# up in that state means running values nobody set - the Guard 1 failure mode with a
# different root cause. We refuse instead.
#
# Usage - all chassis-container compose operations go through this:
#   bash chassis/scripts/compose.sh up -d --force-recreate chassis
#   bash chassis/scripts/compose.sh down
#   bash chassis/scripts/compose.sh logs -f chassis
#   bash chassis/scripts/compose.sh exec chassis bash
#   bash chassis/scripts/compose.sh ps
#   bash chassis/scripts/compose.sh config      # dry-run: prints the resolved stack
#
# Env contract (all optional - every one is derived if unset):
#   CHASSIS_REPO               chassis git tree root, the one holding docker-compose.yml.
#                              If set, it is AUTHORITATIVE - used as-is, and a missing
#                              compose file there is a hard error rather than a silent
#                              fallback to some other repo. If unset, derived: this
#                              script's own path (../.. from chassis/scripts/), else
#                              CHASSIS_HOME, else $HOME/behalfbot - first one holding a
#                              docker-compose.yml wins.
#   CUSTOMER_HOME              customer-state root (holds .env, .env.baked, the override).
#                              Resolved by chassis/scripts/_env.sh.
#   COMPOSE_PROJECT_NAME       compose project to act on. Default `behalfbot`, matching
#                              the `name:` field in the chassis docker-compose.yml.
#                              Confirm what a running install actually uses with:
#                                docker inspect <container> \
#                                  --format '{{index .Config.Labels "com.docker.compose.project"}}'
#   CHASSIS_COMPOSE_OVERRIDE   path to the per-install override file. Default
#                              $CUSTOMER_HOME/chassis-compose.override.yml. Set it to the
#                              empty string to run the chassis stack with no override at
#                              all (chassis dev + smoke-test only, never a real install -
#                              the override is what carries the `env_file:` directive that
#                              puts .env.baked inside the container).
#
# Gotcha when WRITING an override: compose resolves relative paths inside a compose file
# against the PROJECT DIRECTORY, which is the directory of the first `-f` file - i.e. the
# CHASSIS repo, not $CUSTOMER_HOME. So an override saying `env_file: [.env.baked]` looks
# for that file next to docker-compose.yml in the chassis repo and dies with
# "env file ... not found". Always use absolute or interpolated paths in the override:
#   env_file:
#     - path: ${HOME}/.behalfbot/.env.baked
#       required: true

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_env.sh"

: "${CUSTOMER_HOME:?CUSTOMER_HOME could not be resolved - export it (the per-install state root, e.g. ~/.behalfbot)}"

# --- Resolve the chassis repo (Guard 2) ---
# Candidates in precedence order. The first one that actually contains a
# docker-compose.yml wins, so a stale CHASSIS_HOME cannot shadow a good derivation.
# `chassis/scripts/compose.sh` means the repo root is two levels up from SCRIPT_DIR -
# definitionally correct whenever this script is invoked out of a chassis clone.
DERIVED_REPO="$(cd "$SCRIPT_DIR/../.." && pwd)"

# An EXPLICIT CHASSIS_REPO is authoritative - it is never just one candidate among
# several. If the operator names a repo and that repo has no compose file, that is a
# hard error. Letting it fall through to the next candidate is how you end up pointed at
# a DIFFERENT, probably live, install while believing you targeted the one you named -
# the same silent-wrong-target failure Guard 2 and Guard 3 exist to prevent.
if [[ -n "${CHASSIS_REPO:-}" ]]; then
    if [[ ! -f "$CHASSIS_REPO/docker-compose.yml" ]]; then
        echo "ERROR: CHASSIS_REPO=$CHASSIS_REPO has no docker-compose.yml." >&2
        echo "CHASSIS_REPO was set explicitly, so it is used as-is - no fallback." >&2
        echo "Point it at a chassis clone (the tree holding docker-compose.yml), or" >&2
        echo "unset it to let compose.sh derive the repo from its own location." >&2
        exit 2
    fi
    COMPOSE_FILE="$CHASSIS_REPO/docker-compose.yml"
fi

# Only with CHASSIS_REPO unset do we walk the derivation chain. `chassis/scripts/` means
# the repo root is two levels up - definitionally right when run out of a chassis clone.
CHASSIS_REPO_CANDIDATES=("$DERIVED_REPO")
[[ -n "${CHASSIS_HOME:-}" ]] && CHASSIS_REPO_CANDIDATES+=("$CHASSIS_HOME")
CHASSIS_REPO_CANDIDATES+=("$HOME/behalfbot")

if [[ -z "${COMPOSE_FILE:-}" ]]; then
    for candidate in "${CHASSIS_REPO_CANDIDATES[@]}"; do
        if [[ -f "$candidate/docker-compose.yml" ]]; then
            COMPOSE_FILE="$candidate/docker-compose.yml"
            break
        fi
    done
fi

# Legacy layout: the vendored subtree, before behalfbot#136 dropped it. Kept so an
# install that still carries the subtree keeps working untouched.
if [[ -z "${COMPOSE_FILE:-}" && -f "$CUSTOMER_HOME/chassis/docker-compose.yml" ]]; then
    COMPOSE_FILE="$CUSTOMER_HOME/chassis/docker-compose.yml"
fi

if [[ -z "${COMPOSE_FILE:-}" ]]; then
    echo "ERROR: chassis docker-compose.yml not found." >&2
    echo "Looked for docker-compose.yml in, in order:" >&2
    for candidate in "${CHASSIS_REPO_CANDIDATES[@]}"; do
        echo "  $candidate" >&2
    done
    echo "  $CUSTOMER_HOME/chassis   (vendored subtree - dropped in behalfbot#136)" >&2
    echo "" >&2
    echo "Clone the chassis repo, or point CHASSIS_REPO at an existing clone:" >&2
    echo "  CHASSIS_REPO=/path/to/behalfbot bash chassis/scripts/compose.sh $*" >&2
    exit 2
fi

# --- Resolve the per-install override ---
# Unset means "use the default path and require it". Set-but-empty means "deliberately
# no override" - the chassis dev / smoke-test path. `${VAR-}` (no colon) is what tells
# those two apart.
OVERRIDE_FILE="${CHASSIS_COMPOSE_OVERRIDE-$CUSTOMER_HOME/chassis-compose.override.yml}"

if [[ -n "$OVERRIDE_FILE" && ! -f "$OVERRIDE_FILE" ]]; then
    echo "ERROR: $OVERRIDE_FILE not found." >&2
    echo "The per-install override is what carries this install's env_file: directive," >&2
    echo "image pins, port exposure and scaled-to-0 services. Without it the container" >&2
    echo "comes up missing every secret in .env.baked." >&2
    echo "" >&2
    echo "See docs/per-customer-repo-pattern.md. Point CHASSIS_COMPOSE_OVERRIDE at it if" >&2
    echo "it lives elsewhere, or set CHASSIS_COMPOSE_OVERRIDE= (empty) to run the bare" >&2
    echo "chassis stack with no override - chassis development only, never an install." >&2
    exit 2
fi

# --- Guards 1 and 5: the baked env ---
if [[ ! -f "$CUSTOMER_HOME/.env.baked" ]]; then
    echo "ERROR: $CUSTOMER_HOME/.env.baked not found." >&2
    echo "Refusing to invoke docker compose with stale .env contents." >&2
    echo "" >&2
    echo "Bake it first:" >&2
    echo "  CUSTOMER_HOME=$CUSTOMER_HOME bash $SCRIPT_DIR/bake-env.sh" >&2
    exit 2
fi

# A .env edit that never made it into .env.baked is exactly how a container ends up
# running values nobody set. Fail loud.
if [[ -f "$CUSTOMER_HOME/.env" && "$CUSTOMER_HOME/.env" -nt "$CUSTOMER_HOME/.env.baked" ]]; then
    echo "ERROR: $CUSTOMER_HOME/.env is NEWER than $CUSTOMER_HOME/.env.baked." >&2
    echo "The container would start with stale values. Re-bake first:" >&2
    echo "  CUSTOMER_HOME=$CUSTOMER_HOME bash $SCRIPT_DIR/bake-env.sh" >&2
    exit 2
fi

# --- Guard 3: pinned project name ---
PROJECT="${COMPOSE_PROJECT_NAME:-behalfbot}"

# --- Guard 4: CUSTOMER_HOME must reach compose's interpolator ---
export CUSTOMER_HOME

COMPOSE_ARGS=(-p "$PROJECT" --env-file="$CUSTOMER_HOME/.env.baked" -f "$COMPOSE_FILE")
[[ -n "$OVERRIDE_FILE" ]] && COMPOSE_ARGS+=(-f "$OVERRIDE_FILE")

exec docker compose "${COMPOSE_ARGS[@]}" "$@"
