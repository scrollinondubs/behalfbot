"""Background handoff to Jax, plus delivery of what comes back.

Escalation must never block the conversation. The router says something short,
the work happens on a background task, and Sean keeps talking. When an answer
lands it goes two places: spoken aloud if the conversation is idle, and posted
to Discord regardless, so walking away does not lose it.

Which Discord channel is a privacy decision, not a convenience one:

  JAX answers -> #jax, the ordinary work channel.

There is no Jill path here any more, and its absence is the point. The voice
loop cannot call Venice, cannot post to a private channel, and cannot read one.
When the router refuses a turn (Route.HELD) nothing is sent anywhere at all -
the bot says so out loud and Sean takes the question to Jill himself, which is
the boundary that was working before a classifier was put in the middle of it.

Deleted rather than disabled by a flag, so that no future change can turn it
back on without someone deciding to.

This also settles a rule that used to constrain this file. `docs/privacy-boundary.md`
forbids posting private-workflow output to any channel other than #jax-private,
which is why the old code posted Venice's answers there. With no private
workflow running here there is nothing to post anywhere, and #jax is the only
channel this module knows.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

from router import Route

def _repo_root() -> Path:
    """Where the Claude Code install lives, which is not always our parent.

    `claude -p` needs `.mcp.json` and `scripts/claude-p-with-telemetry.sh`, and
    `.mcp.json` is untracked - it exists in the install at ~/.behalfbot and in
    no git worktree cut from it. Assuming `parent.parent` was correct only while
    this package sat inside the install; run from a worktree it resolved to the
    worktree root and every escalation died with "MCP config file not found",
    which is exactly how it failed live on 2026-08-07.

    So look for the file that actually has to be there, and fall back to the
    install rather than to whatever directory we happen to be checked out in.
    """
    override = os.getenv("VOICE_REPO_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    here = Path(__file__).resolve().parent
    for candidate in (here, *here.parents):
        if (candidate / ".mcp.json").is_file():
            return candidate
    return Path.home() / ".behalfbot"


REPO = _repo_root()
JAX_CHANNEL = "1487190325394014432"       # #jax
DEVOPS_CHANNEL = "1497870976237699173"    # #jax-devops - operational noise only
DISCORD_TOKEN_ENV = Path.home() / ".claude" / "channels" / "discord" / ".env"

# Discord delivery is opt-in and off unless the launcher asks for it. run.sh
# sets it; nothing else does, so no eval, benchmark, test or ad-hoc import of
# this module can page a human.
#
# This is not belt-and-braces. An earlier revision of the confirmation gate
# posted a notice on every declined read-back and did it from a code path the
# dry-run flag did not cover, so a three-minute benchmark run put eight
# notifications in front of Sean. A flag on the escalation is the wrong place
# for that guard - it has to sit on the delivery call itself, which is the one
# thing every path to Discord has in common.
DISCORD_ENABLED = os.getenv("VOICE_DISCORD", "0") == "1"

# Which MCP servers the Jax path is allowed to boot.
#
# `claude -p --mcp-config .mcp.json` starts every server in that file before it
# answers a word, and the repo config has fifteen. Measured on this box, same
# question ("what is 17 times 3") through the same binary:
#
#   no servers                     4 s
#   these four                     7 s
#   all fifteen except playwright  10 s
#   all fifteen                    killed at 420 s, no answer
#
# Playwright on its own is the difference between ten seconds and never. It is
# excluded here for that reason, not because a spoken question would never want
# a browser.
#
# The four kept are the ones a spoken question actually reaches for: memory for
# recall, github for the issue queue, turso for tickets and members, and
# gmail-monitor for mail. Everything else is a slow answer waiting to happen.
# Set the variable to an empty string to run with no MCP servers at all, which
# is the fastest and answers less.
JAX_MCP_SERVERS = os.getenv(
    "VOICE_JAX_MCP_SERVERS", "memory,github,turso,gmail-monitor"
)

# Carry one claude session across a whole voice conversation, so turn two knows
# what turn one was about. Off gives the old behaviour: every escalation cold.
#
# Cheaper as well as better. A resumed session does not re-pay for CLAUDE.md and
# the MCP tool definitions on every question.
STICKY_SESSION = os.getenv("VOICE_JAX_STICKY_SESSION", "1") != "0"

JAX_TIMEOUT_SECS = float(os.getenv("VOICE_JAX_TIMEOUT_SECS", "300"))
JAX_MODEL = os.getenv("VOICE_JAX_MODEL", "sonnet")
JAX_BUDGET_USD = os.getenv("VOICE_JAX_BUDGET_USD", "0.50")
# Benchmarking the confirmation gate means driving the Jax path over and over.
# Doing that for real would start an agent run per measurement and post to
# Discord each time, so the benchmarks set this and nothing leaves the box.
DRY_RUN = os.getenv("VOICE_ESCALATION_DRYRUN", "0") == "1"

# Spoken the instant a route resolves, before any work starts. Deliberately
# boring: it is the only thing standing between Sean and a silent pause, so it
# has to be fast to synthesize, not clever.
ACKS = {
    Route.JAX: "On it.",
}

# What the bot says when the keyword net refuses a turn. It has to be
# unambiguous that nothing was sent and that the next move is Sean's, because
# the failure mode of a vague line here is him assuming it was handled.
HELD_LINE = "That sounds private, so I have not sent it anywhere. Take it to Jill."

ANSWER_PREFIX = {
    Route.JAX: "Jax says.",
}

SPOKEN_ANSWER_CHARS = 700  # past this the spoken version is a pointer to Discord


@dataclass
class Escalation:
    route: Route
    utterance: str
    started: float = field(default_factory=time.time)
    answer: str | None = None
    error: str | None = None

    @property
    def elapsed(self) -> float:
        return time.time() - self.started


# --- delivery -------------------------------------------------------------

def _discord_token() -> str | None:
    if not DISCORD_TOKEN_ENV.exists():
        return None
    for line in DISCORD_TOKEN_ENV.read_text().splitlines():
        if line.startswith("DISCORD_BOT_TOKEN="):
            return line.split("=", 1)[1].strip()
    return None


def _post_discord(channel_id: str, content: str) -> bool:
    """Post one message, splitting at Discord's 2000-char limit."""
    import urllib.error
    import urllib.request

    if not DISCORD_ENABLED or DRY_RUN:
        # Length, not content: this line is for confirming a message would have
        # gone, and the private channel's payload has no business in a log that
        # exists to debug delivery.
        logger.info(f"discord suppressed - {len(content)} chars to {channel_id}")
        return False

    token = _discord_token()
    if not token:
        logger.warning("no Discord bot token; escalation answer not posted")
        return False

    ok = True
    chunks = [content[i:i + 1900] for i in range(0, len(content), 1900)] or [content]
    for chunk in chunks:
        req = urllib.request.Request(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            data=json.dumps({"content": chunk,
                             "allowed_mentions": {"parse": []}}).encode(),
            headers={"Authorization": f"Bot {token}",
                     "Content-Type": "application/json",
                     "User-Agent": "jax-voice-router (1.0)"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15):
                pass
        except urllib.error.HTTPError as e:
            logger.error(f"discord post failed: HTTP {e.code}")
            ok = False
        except Exception as e:  # noqa: BLE001
            logger.error(f"discord post failed: {e}")
            ok = False
    return ok


def deliver_to_discord(esc: Escalation) -> None:
    """Post an answer to #jax. Only JAX escalations ever get here.

    A HELD turn never becomes an Escalation, so there is no branch for it and no
    private channel referenced anywhere in this module.
    """
    if esc.route is not Route.JAX:
        logger.error(f"refusing to deliver a {esc.route.value} escalation")
        return
    body = esc.answer or f"(escalation failed: {esc.error})"
    _post_discord(JAX_CHANNEL, f"**voice** - {esc.utterance}\n\n{body}")


CONFIRM_LOG = Path(__file__).parent / "metrics" / "confirmations.jsonl"


def record_confirmation_drop(reason: str) -> None:
    """Write a blocked escalation to a local file, and notify nobody.

    A declined read-back is the gate working. Sean said no out loud, heard the
    bot say it was dropped, and nothing was sent - there is no action for anyone
    to take, so there is nothing to interrupt him with. The same is true of a
    timeout and of an unrecognised answer: the safe thing happened, and he was
    in the room while it did. An earlier revision of this posted to Discord and
    put eight notifications in front of him inside three minutes, which is how a
    correct system teaches someone to ignore it.

    The record still has to exist, because "did that ever actually go?" is a
    real question. It just belongs in a file. If a notice is ever genuinely
    warranted here it goes to #jax-devops (DEVOPS_CHANNEL), never #jax.

    The utterance is deliberately not written down either. The thing being
    blocked might have been a misrouted private question, and the point of the
    block was to keep it off the wire.
    """
    try:
        CONFIRM_LOG.parent.mkdir(parents=True, exist_ok=True)
        with CONFIRM_LOG.open("a") as f:
            f.write(json.dumps({"ts": time.time(), "reason": reason,
                                "sent": False}) + "\n")
    except Exception as e:  # noqa: BLE001 - an audit line is never worth a crash
        logger.warning(f"could not record confirmation drop: {e}")


# --- the two back ends ----------------------------------------------------

JAX_PROMPT = """This came in by voice, so answer the way you would say it out \
loud: plain sentences, no markdown, no lists, no code blocks, under about \
eighty words unless the question genuinely needs more. Do the work first - use \
your tools, check the repo, read memory - then give the answer, not a \
description of how you got it.

Sean asked: {utterance}"""


_MCP_CONFIG_CACHE: Path | None = None


def voice_mcp_config() -> Path | None:
    """Write a cut-down copy of the repo's MCP config and return its path.

    Derived from `.mcp.json` on every call rather than kept as a second file on
    disk, because that file carries tokens and one copy of a secret is enough.
    Returns None when the allowlist is empty or the repo config is unreadable,
    which the caller turns into "run with no MCP servers" rather than a failure:
    an answer without tools beats no answer.
    """
    global _MCP_CONFIG_CACHE
    if _MCP_CONFIG_CACHE is not None and _MCP_CONFIG_CACHE.is_file():
        return _MCP_CONFIG_CACHE

    wanted = [s.strip() for s in JAX_MCP_SERVERS.split(",") if s.strip()]
    if not wanted:
        return None

    src = REPO / ".mcp.json"
    try:
        servers = json.loads(src.read_text()).get("mcpServers", {})
    except (OSError, ValueError) as e:
        logger.warning(f"could not read {src}, running without MCP: {e}")
        return None

    kept = {name: servers[name] for name in wanted if name in servers}
    missing = [name for name in wanted if name not in servers]
    if missing:
        logger.warning(f"MCP servers not in {src.name}: {', '.join(missing)}")
    if not kept:
        return None

    out = REPO / "logs" / "voice-mcp-config.json"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"mcpServers": kept}))
        out.chmod(0o600)
    except OSError as e:
        logger.warning(f"could not write {out}, running without MCP: {e}")
        return None

    logger.info(f"voice MCP config: {len(kept)} of {len(servers)} servers")
    _MCP_CONFIG_CACHE = out
    return out


async def _run_claude(utterance: str, session_id: str | None,
                      resume: bool) -> tuple[str, str | None]:
    """One claude -p invocation. Returns (answer, session id it ran under)."""
    cmd = [
        "bash", str(REPO / "scripts" / "claude-p-with-telemetry.sh"), "voice-router",
        "-p", JAX_PROMPT.format(utterance=utterance),
        "--dangerously-skip-permissions",
        "--model", JAX_MODEL,
        "--max-budget-usd", JAX_BUDGET_USD,
        "--output-format", "json",
    ]
    if session_id and resume:
        cmd += ["--resume", session_id]
    elif session_id:
        cmd += ["--session-id", session_id]
    # --strict-mcp-config so the trimmed list is the whole list. Without it the
    # user-level and project-level configs are merged back in and playwright
    # returns through the side door.
    mcp = voice_mcp_config()
    if mcp is not None:
        cmd += ["--strict-mcp-config", "--mcp-config", str(mcp)]
    else:
        cmd += ["--strict-mcp-config"]
    # stdin=DEVNULL is load-bearing, not tidiness. claude-p-with-telemetry.sh
    # does `if [[ ! -t 0 ]]; then cat > "$TMP_STDIN"; fi` to measure prompt
    # size, and that `cat` blocks until EOF. Inheriting a pipe that nobody ever
    # closes hangs the wrapper before claude is even exec'd, which is a hang
    # with no output, no error and no clue - it just sits there until
    # JAX_TIMEOUT_SECS. DEVNULL gives it immediate EOF.
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(REPO),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=JAX_TIMEOUT_SECS)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"jax timed out after {JAX_TIMEOUT_SECS:.0f}s") from None

    if proc.returncode != 0:
        raise RuntimeError(f"claude -p exit {proc.returncode}: "
                           f"{err.decode(errors='replace')[-300:]}")

    text = out.decode(errors="replace").strip()
    # The wrapper tees claude's stdout, so the JSON envelope is the last line
    # that parses. Fall back to the raw text if the shape ever changes.
    for line in reversed(text.splitlines()):
        try:
            blob = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(blob, dict) and blob.get("result"):
            return str(blob["result"]).strip(), blob.get("session_id") or session_id
    return text, session_id


async def ask_jax(utterance: str, session_id: str | None = None) -> tuple[str, str | None]:
    """Hand the question to the Claude Code install and wait for the answer.

    Returns (answer, session id) so the caller can carry the session into the
    next turn. Passing one back in is what makes a voice exchange a conversation
    rather than a series of unrelated questions: without it every escalation is
    a cold instance that has never heard the turn before it, so "what about the
    second one" reaches somebody with no idea there was a first.

    Sticky sessions were not possible before 2026-08-07 and the reason was the
    privacy boundary, not the plumbing. Earlier turns might have gone to Venice,
    and carrying the transcript here would have sent Anthropic exactly what the
    routing existed to keep from it. With Jill out of the loop there is nothing
    in a voice conversation being withheld - every turn either stayed on the box
    or came here - so there is no longer anything to leak by remembering.

    A resume that fails starts a fresh session and retries once. Sessions expire,
    get cleaned up, or belong to a different working directory, and none of those
    is a reason for Sean to get an error instead of an answer.
    """
    if not STICKY_SESSION:
        answer, _ = await _run_claude(utterance, None, resume=False)
        return answer, None

    if session_id:
        try:
            return await _run_claude(utterance, session_id, resume=True)
        except RuntimeError as e:
            logger.warning(f"resume of {session_id} failed, starting fresh: {e}")

    fresh = str(uuid.uuid4())
    return await _run_claude(utterance, fresh, resume=False)


# There was an ask_jill() here that called Venice. It is gone, not disabled.
#
# The reasoning is in the router docstring: a 4B classifier standing where Sean
# used to stand is a probabilistic gate on a privacy boundary, and a boundary
# that holds three times in four is worse than none, because it makes the person
# in front of it relaxed. A voice loop that can reach Venice will eventually
# reach it wrongly. One that has no code path there cannot.
#
# The feature it removes was never coherent either: Venice's answer would have
# come back through this box, been spoken aloud and landed in a transcript,
# crossing the boundary in the direction nobody was watching.
#
# Jill is unchanged and still reachable. Sean goes to her channel, the way he
# did before any of this existed.

# --- the queue the bot drains --------------------------------------------

class Escalator:
    """Fire-and-forget escalations with an out-queue of finished answers."""

    def __init__(self) -> None:
        self.answers: asyncio.Queue[Escalation] = asyncio.Queue()
        self.in_flight: set[asyncio.Task] = set()
        # The claude session this conversation is using. One Escalator is built
        # per WebRTC connection, which makes the lifetime exactly right: turns
        # within a conversation share a session, and reconnecting starts clean
        # rather than resuming something from hours ago.
        #
        # Set after the first escalation answers, because that is when claude
        # tells us which session it actually ran under.
        self.session_id: str | None = None
        # Serialises escalations within a conversation. Two claude -p processes
        # resuming the same session concurrently would interleave writes into
        # one transcript; the second question also usually depends on the first
        # having landed, so queueing is the behaviour Sean wants anyway.
        self._session_lock = asyncio.Lock()

    def start(self, route: Route, utterance: str) -> str:
        """Kick off the handoff. Returns the line to say right now.

        JAX is the only route that can be started. HELD means the turn was
        refused and nothing is sent, so reaching here with it is a caller bug -
        raised rather than logged, because the quiet version of this mistake is
        a refused question escalating anyway.
        """
        if route is not Route.JAX:
            raise ValueError(f"cannot escalate {route.value}; only JAX is sendable")
        esc = Escalation(route=route, utterance=utterance)
        task = asyncio.create_task(self._run(esc))
        self.in_flight.add(task)
        task.add_done_callback(self.in_flight.discard)
        return ACKS[route]

    async def _run(self, esc: Escalation) -> None:
        if DRY_RUN:
            logger.warning(f"DRY RUN - {esc.route.value} not called, nothing posted")
            esc.answer = f"Dry run. {esc.route.value} was not called."
            await self.answers.put(esc)
            return
        try:
            async with self._session_lock:
                esc.answer, self.session_id = await ask_jax(
                    esc.utterance, self.session_id)
            logger.info(f"{esc.route.value} answered in {esc.elapsed:.1f}s")
        except Exception as e:  # noqa: BLE001
            esc.error = f"{type(e).__name__}: {e}"
            logger.error(f"{esc.route.value} escalation failed: {esc.error}")
        # Discord first: it is the delivery that survives Sean walking away.
        await asyncio.to_thread(deliver_to_discord, esc)
        await self.answers.put(esc)

    @staticmethod
    def spoken_form(esc: Escalation) -> str:
        """What to say when the answer arrives and the room is quiet."""
        if esc.error:
            # Not "it's in Discord". A failed escalation delivered nothing
            # anywhere, and sending Sean to look for a message that does not
            # exist is worse than saying plainly that it did not happen.
            return f"{ANSWER_PREFIX[esc.route]} That one failed. Nothing was sent."
        answer = esc.answer or ""
        if len(answer) > SPOKEN_ANSWER_CHARS:
            where = "#jax-private" if esc.route is Route.HELD else "Discord"
            return (f"{ANSWER_PREFIX[esc.route]} That's a long one, "
                    f"I've put it in {where}.")
        return f"{ANSWER_PREFIX[esc.route]} {answer}"
