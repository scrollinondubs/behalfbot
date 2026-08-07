"""Router - decides whether a spoken utterance is answered here or escalated.

Two destinations, and one refusal:

  CHAT  answered by the local model itself, in the voice loop, ~1s
  JAX   escalated to the Claude Code install (tools, memory, repos, business)
  HELD  nothing is sent anywhere; Sean is told to take it to Jill himself

WHY JILL IS NOT A DESTINATION ANY MORE (2026-08-07)
---------------------------------------------------
She was. The router had three destinations and decided, per utterance, whether
something was private enough to go to Venice instead of Anthropic. That is the
design this file used to describe at length, and it was wrong in a way that took
a while to see.

The privacy boundary that actually works in this system is **Sean**. When a
question needs Jill, he carries it into her channel himself, reads her answer,
and carries back whatever he chooses to. Nothing automated touches the membrane.
It fails closed by construction, because nothing crosses unless he personally
moves it, and he is the only party that understands the context well enough to
judge.

Putting a 4B classifier in that position does not make a smaller version of the
same boundary. It makes a different kind of object: a probabilistic gate on a
privacy decision. Measured on the conversation set, that gate let 25% of private
turns through to Anthropic.

The number is not the point, though. **A boundary that is 75% reliable is worse
than no boundary at all**, because of what it does to the person in front of it.
No boundary makes Sean careful, and careful is what has kept this safe for
months of typing into Discord where no router has ever existed. A boundary he
believes in makes him relaxed, and relaxed in front of a leaky gate is strictly
more exposure than careful in front of none.

So the voice loop does not talk to Venice. It cannot: there is no code path from
here to Jill, deliberately, so that no future change can quietly reintroduce
one. What is left is the thing that was actually wanted - a fast local model
that fields what it can and escalates what it cannot - with the same trust model
Sean already operates when he types.

The keyword net survives, inverted. It no longer routes anything; it can only
refuse. If an utterance looks private it is HELD, the bot says so, and Sean
takes it to Jill in her channel. The one thing it can do is stop, which means
it cannot leak, which means it can be trusted with the one job it has: catching
the case where he was mid-flow and never decided anything at all.

ONE SPEAKER IS THE WHOLE ASSUMPTION
-----------------------------------
The above holds because Sean is the only voice this thing ever hears. He is
trusted, he can steer by naming a destination, and everything else defaults to
an escalation that runs an agent with tools on his behalf.

None of that survives a phone call. The moment this endpoint answers an inbound
line, the speaker is untrusted and every layer inverts:

  - Spoken routing overrides must NOT be honoured. "Ask Jax about the bank
    account" from a caller is an instruction to a tool, not an address.
  - The default must NOT be JAX. An escalation runs an agent with tools against
    the repo, memory and mail on behalf of whoever is talking.
  - The confirmation gate authenticates nobody. It already does not - see the
    note at the end of confirm.py - and against a caller reading the prompt back
    it is worse than useless, because it looks like authorisation.

The correct shape for that case is a separate mode: local model only, no
escalation, no tools, no overrides. It is not a flag on this design, and adding
the phone leg without building it would hand an unknown caller the same
authority Sean has. Written down here rather than in a ticket because the person
who wires up Telnyx will read this file first.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import httpx

LLM_BASE_URL = os.getenv("VOICE_LLM_BASE_URL", "http://127.0.0.1:11434/v1")
OLLAMA_URL = LLM_BASE_URL.removesuffix("/v1")
# How long Ollama keeps the router voters resident between turns. "-1" pins
# them forever; a duration string like "30m" lets them fall out eventually.
#
# Pinned by default, deliberately. Any finite value just moves the cold-load
# cliff further out: walk away for longer than it and the next question pays a
# 4 s classify and the turn falls through to an escalation it did not need.
# The router is about 3 GB on a 24 GB box that is already holding llama3.2,
# whisper and kokoro, so the memory is affordable and the stall is not.
# Ollama accepts either a duration string ("30m") or a number of seconds, and
# -1 for "never unload" is only valid as the number. Sent as the string "-1" it
# returns 400 and the bot dies at startup, which is exactly what happened here
# on the first attempt. So parse it: digits become an int, anything else goes
# through as the duration string it is.
def _keep_alive(raw: str) -> int | str:
    try:
        return int(raw)
    except ValueError:
        return raw


KEEP_ALIVE = _keep_alive(os.getenv("VOICE_ROUTER_KEEP_ALIVE", "-1"))
ROUTER_MODEL = os.getenv("VOICE_ROUTER_MODEL", "gemma3:4b")
# The second voter, off by default since 2026-08-07.
#
# It existed for one job: catch a private question the primary model was about
# to send to Anthropic, by letting either voter's JILL count on its own. With
# Venice out of the loop there is no privacy verdict left for it to cast, and
# the only thing it can still do is pull a JAX down to CHAT - which trades a
# correct answer for a fast wrong one and costs 200-460 ms of contention to do
# it. Set a model name here to bring it back.
ROUTER_MODEL_2 = os.getenv("VOICE_ROUTER_MODEL_2", "off")
ROUTER_TIMEOUT_SECS = float(os.getenv("VOICE_ROUTER_TIMEOUT_SECS", "4.0"))
# The keyword net is a safety layer, not the classifier. Off is a valid choice
# if it ever over-fires; the measured cost of turning it off is in the README.
PREFILTER_ENABLED = os.getenv("VOICE_ROUTER_PREFILTER", "1") != "0"



def second_model(name: str) -> str | None:
    """Normalize a second-voter setting. None means single-model routing."""
    name = (name or "").strip()
    return None if name.lower() in ("", "off", "none", "0") else name


class Route(str, Enum):
    CHAT = "CHAT"
    JAX = "JAX"
    HELD = "HELD"


# Where an utterance goes when nobody was named and the keyword net stayed
# quiet. JAX, and there is no longer another off-box destination to weigh it
# against - the only alternatives are answering locally or refusing.
#
# The disclosure this accepts, stated plainly so nobody has to reconstruct it:
# escalating sends the transcript to Anthropic. The words are the payload, and
# nothing downstream of the routing decision can take them back. That is the
# same exposure Sean has every time he types into Discord, managed the same way
# - by him, before he speaks.
DEFAULT_ROUTE = Route.JAX

# Whether a held thread keeps holding through its follow-ups.
#
# On by default and it matters more than it looks. "I was at the doctor this
# morning" trips the keyword net; "what were the numbers they read out" one
# sentence later does not, and without this it would escalate. Measured on the
# conversation set, follow-up holding is the difference between catching half a
# private thread and catching all of it.
#
# The way out is the way in: Sean names Jax, or changes subject to something the
# classifier reads as ordinary.
STICKY_WHOLE_THREAD = os.getenv("VOICE_ROUTER_STICKY_THREAD", "1") != "0"


@dataclass
class Decision:
    route: Route
    # "override" | "sticky" | "prefilter" | "prefilter-ask" | "model" | "fallback"
    source: str
    latency_ms: float
    raw: str = ""
    # model name -> the route it voted for, or None when it failed to answer.
    votes: dict[str, Route | None] = field(default_factory=dict)
    # True when the keyword net fired on something Sean did not address to
    # anyone. `route` still says where it would go, but nothing may leave the
    # box until he answers "Jill or Jax". This is the one place the privacy
    # boundary still interrupts him, and it is deliberately the only one.
    ask: bool = False


# --- layer 1: explicit override -------------------------------------------
#
# "ask Jax about X" wins outright. Sean saying a name is a stronger signal than
# anything the classifier can infer, and honouring it is what makes the router
# feel steerable rather than arbitrary.
#
# Whisper renders both names loosely and the confirmation gate depends on the
# same list, so the variants live here and nowhere else. "jail" is in the JILL
# set because that is literally what `whisper-base` produced for "send it to
# Jill" in bench_confirm, five times out of five - and with the gate reading a
# redirect as consent, that one transcription error was enough to send a
# question to Anthropic that Sean had just asked to keep off it.
JAX_NAMES = r"(?:jax|jacks|jaxx|jack|jags|jx)"
JILL_NAMES = r"(?:jill|jil|jils|jyl|jell|gil|gill|jail|jails)"

_ADDRESS = r"(?:ask|check\s+with|get|have|tell|hey|yo|okay|ok)\s+"
_OVERRIDE_JAX = re.compile(rf"\b{_ADDRESS}{JAX_NAMES}\b", re.I)
_OVERRIDE_JILL = re.compile(rf"\b{_ADDRESS}{JILL_NAMES}\b", re.I)
# Bare vocative at the head of the utterance: "Jax, what's on my queue".
#
# The punctuation is optional and so is a possessive tail, because both are
# Whisper's choice rather than Sean's. Live, whisper-base rendered the same
# spoken sentence as "Jack, I'm testing..." once and "Jack's what's in my
# GitHub queue?" the next time. Requiring a comma made the first an explicit
# address and the second an inferred guess, so the gate fired on one and not
# the other for input Sean spoke identically. A trailing space is still
# required, so a one-word "Jax." is not a vocative.
_VOCATIVE_TAIL = r"(?:'s|s)?\s*[,:.!?]?\s+"

# Discourse openers that carry no meaning and are not addressed to anybody.
# Stripped before the vocative is looked for, because anchoring at ^ made the
# address layer lose a name behind any of them. Live failure, 2026-08-07: Sean
# said "Alright, Jack, I'm testing this voice interface" and the override never
# fired - "Jack" was neither at position zero nor after one of the _ADDRESS
# verbs - so an utterance he had explicitly addressed to Jax fell through to the
# classifier and landed on Jill.
_LEADING_FILLER = re.compile(
    r"^\s*(?:alright|all right|ok|okay|so|well|now|right|and|but|hey|yo|um|uh|"
    r"erm|hmm|listen|look)\b[,:;.!?]?\s+", re.I)


def strip_leading_filler(text: str, max_strips: int = 4) -> str:
    """Drop meaningless openers so an anchored match can see past them.

    Bounded rather than looped to exhaustion: an utterance that is nothing but
    filler should stay nothing but filler, not become an empty string that the
    caller then treats as a different kind of input.
    """
    for _ in range(max_strips):
        stripped = _LEADING_FILLER.sub("", text, count=1)
        if stripped == text:
            break
        text = stripped
    return text


_VOCATIVE_JAX = re.compile(rf"^\s*{JAX_NAMES}{_VOCATIVE_TAIL}", re.I)
_VOCATIVE_JILL = re.compile(rf"^\s*{JILL_NAMES}{_VOCATIVE_TAIL}", re.I)

# --- layer 0: transcription repair ----------------------------------------
#
# Whisper hears a name and writes a different word. Sean says a shorthand and
# Whisper writes it faithfully, but no model maps "C1" to SiYuan. Both end the
# same way: the router classifies a sentence Sean did not say. Repairing the
# text once, before anything reads it, is cheaper and far more predictable than
# teaching every downstream layer the variants.
#
# Applied to the transcript itself rather than only to the routing copy, so the
# read-back quotes the repaired sentence and the escalation asks the repaired
# question. Sean hears what is about to be sent.

ALIASES_PATH = Path(
    os.getenv("VOICE_ALIASES_PATH", str(Path(__file__).parent / "aliases.json")))

_ALIAS_RE: re.Pattern[str] | None = None
_ALIAS_MAP: dict[str, str] = {}


def _load_aliases() -> tuple[re.Pattern[str] | None, dict[str, str]]:
    global _ALIAS_RE, _ALIAS_MAP
    if _ALIAS_RE is not None:
        return _ALIAS_RE, _ALIAS_MAP
    try:
        raw = json.loads(ALIASES_PATH.read_text())
    except (OSError, ValueError):
        # No alias file is a valid configuration, not an error. The router still
        # works; it just hears what Whisper heard.
        _ALIAS_RE = re.compile(r"(?!x)x")  # matches nothing
        return _ALIAS_RE, _ALIAS_MAP
    _ALIAS_MAP = {
        k.lower(): v for k, v in raw.items()
        if not k.startswith("_") and isinstance(v, str)
    }
    if not _ALIAS_MAP:
        _ALIAS_RE = re.compile(r"(?!x)x")
        return _ALIAS_RE, _ALIAS_MAP
    # Longest first so "see one" wins over any future "one", and \b at both ends
    # so a substitution cannot fire inside a longer word.
    keys = sorted(_ALIAS_MAP, key=len, reverse=True)
    _ALIAS_RE = re.compile(r"\b(?:" + "|".join(re.escape(k) for k in keys) + r")\b", re.I)
    return _ALIAS_RE, _ALIAS_MAP


def normalize_transcript(text: str) -> str:
    """Apply the alias table to one utterance. Never raises."""
    if not text:
        return text
    pattern, mapping = _load_aliases()
    if not mapping:
        return text
    return pattern.sub(lambda m: mapping[m.group(0).lower()], text)


def explicit_override(text: str) -> Route | None:
    """Return the route Sean named out loud, or None if he named nobody.

    Jill is checked first so "ask Jill, not Jax" resolves the careful way. She
    is not a destination any more, so naming her returns HELD: the turn stops
    here and Sean is told to take it to her channel himself. That is not a
    downgrade of the feature, it is the feature - a spoken question answered by
    Venice would have to come back through this box to be read aloud, which is
    the boundary crossing in the other direction.
    """
    head = strip_leading_filler(text)
    if _OVERRIDE_JILL.search(text) or _VOCATIVE_JILL.search(head):
        return Route.HELD
    if _OVERRIDE_JAX.search(text) or _VOCATIVE_JAX.search(head):
        return Route.JAX
    return None


# --- layer 2: sensitive keyword net ---------------------------------------
#
# Deterministic, zero-latency, and deliberately trigger-happy. It exists to
# catch the cases a 7B model gets wrong, and its false positives cost only a
# slower answer from Venice. Measured contribution is in the README.

_SENSITIVE_TERMS = [
    # tax and accounting
    r"\btax(?:es|ed)?\b", r"\birs\b", r"\bnif\b", r"\bvat\b", r"\biva\b",
    r"\bfinan[cç]as\b", r"\baccountant\b", r"\bbookkeep", r"\bdeduct",
    r"\bwrite[- ]off", r"\bfiscal\b", r"\btaxable\b", r"\bhmrc\b",
    # money held, earned, owed
    r"\bbank\b", r"\biban\b", r"\bnet worth\b",
    r"\b(?:bank|business|checking|savings|current|joint|company)\s+account\b",
    r"\bhow much is (?:in|left)\b", r"\bhow much (?:is|do) (?:i|we) (?:have|owe)\b",
    r"\bsalary\b", r"\bincome\b", r"\bpayroll\b", r"\bmortgage\b",
    r"\bloan\b", r"\bdebt\b", r"\binvest(?:ment|ing|ed)?\b", r"\bportfolio\b",
    r"\bbrokerage\b", r"\bpension\b", r"\bretirement\b", r"\bcrypto wallet\b",
    r"\bseed phrase\b", r"\bbalance on\b", r"\bcredit card\b", r"\bstatement\b",
    # money spent
    r"\bspen[dt]\b", r"\bspending\b", r"\bcost me\b", r"\bhow much (?:did|do|does) (?:i|we|it)\b",
    r"\bwhat did (?:i|we) pay\b", r"\bexpenses?\b", r"\binvoice", r"\breceipts?\b",
    # legal
    r"\bbeneficiar", r"\bnext of kin\b", r"\bpower of attorney\b",
    r"\blawyer\b", r"\battorney\b", r"\badvogad", r"\bnotary\b", r"\bnot[aá]rio\b",
    r"\bcontract\b", r"\blease\b", r"\bdeed\b", r"\bwill\b and\b", r"\btestament\b",
    r"\bcustody\b", r"\bdivorce\b", r"\bsettlement\b", r"\blitigation\b", r"\bsue\b",
    r"\bvisa\b", r"\bresidency\b", r"\bimmigration\b", r"\bcitizenship\b", r"\baima\b",
    r"\binsurance\b", r"\bpolicy number\b", r"\bpremium\b", r"\bclaim\b",
    # medical
    r"\bdoctor\b", r"\bm[eé]dico\b", r"\bprescri", r"\bdiagnos", r"\bsymptom",
    r"\bblood (?:test|work|panel|pressure)\b", r"\bcholesterol\b", r"\bmri\b",
    r"\bx[- ]?ray\b", r"\bbiopsy\b", r"\bmedical record", r"\bhealth record",
    r"\btherapist\b", r"\btherapy\b", r"\bpsychiatr", r"\bantidepressant",
    r"\bmedication\b", r"\bdosage\b", r"\bmy meds\b", r"\bhospital\b", r"\bclinic\b",
    r"\bsurgery\b", r"\bspecialist\b", r"\bsns\b", r"\butente\b",
    r"\bscan\b", r"\bultrasound\b", r"\bdentist\b", r"\bdental\b", r"\bpharmac",
    r"\ballerg", r"\bblood type\b", r"\bvaccin", r"\bimmuni[sz]ation\b",
    r"\bmy (?:results?|chart|file|history)\b", r"\bmedical history\b",
    r"\bappointment\b", r"\brefill\b", r"\bconsult(?:ation)?\b", r"\bsick\b",
    r"\binjur", r"\bpain in my\b", r"\bmy (?:knee|back|chest|stomach|heart)\b",
    # identity and credentials
    r"\bpassport\b", r"\bpassword\b", r"\bpasswords\b", r"\bcredential",
    r"\bsocial security\b", r"\bssn\b", r"\bid number\b", r"\bdate of birth\b",
    r"\bapi key\b", r"\bprivate key\b", r"\b2fa\b", r"\bpin code\b",
    # the property, which is a money-and-paperwork object
    r"\bquinta\b", r"\bproperty tax", r"\bimi\b", r"\bcondo fee", r"\bhoa\b",
    # Added 2026-08-07 after the conversation eval. Every one of these is a
    # thread that stayed private for several turns and was never caught,
    # because the opening line named no regulated noun - it was a builder's
    # quote, a domestic wage, or the safe. A false positive here costs one
    # repeated sentence, so the bar for adding a term is low on purpose.
    r"\bquote(?:d|s)?\b", r"\bestimate(?:d|s)?\b", r"\bthe builder\b",
    r"\bwhat (?:do|did) (?:i|we) (?:pay|agree)\b", r"\bpay (?:her|him|them)\b",
    r"\bthe cleaner\b", r"\bwages?\b", r"\bcash\b",
    r"\bthe safe\b", r"\bcombination\b", r"\bthe code for\b",
    r"\bwhat'?s left to pay\b", r"\bstill owe\b", r"\bowe(?:d|s)?\b",
]
_SENSITIVE_RE = re.compile("|".join(_SENSITIVE_TERMS), re.I)


def sensitive_prefilter(text: str) -> bool:
    return bool(_SENSITIVE_RE.search(text))


# --- layer 3: the local classifier ----------------------------------------
#
# Kept byte-identical on every call so Ollama's prompt cache holds the prefix
# and each classification is prompt-eval-free. The utterance is the only thing
# that changes, and it goes last.

ROUTER_SYSTEM = """You decide who answers Sean's spoken request. \
Answer with ONE word and nothing else: CHAT or JAX.

JAX handles anything needing live tools, stored memory, code, or the business:
GitHub issues and pull requests, code, deploys, servers, containers, the
website, Vibecode Lisboa, MicroAdventures, prep.training, leads, students,
cohorts, outreach, email drafts, the calendar, Discord, briefings, the blog and
content pipeline, Oura sleep and readiness numbers, workouts he logged, the
dating queue, home automation, notes in SiYuan, and anything phrased as "do I
have", "what's on my", "did I", "remind me", "draft", "send", "check", "look up
in my", or naming a person, project or repo that belongs to Sean.

CHAT is everything else: general knowledge, facts about the world, definitions,
directions and distances, weather, recipes, history, science, opinions, jokes,
small talk, and questions about you.

Decide by whether answering needs anything of Sean's.
- Needs his tools, projects, records or work data -> JAX.
- Needs nothing of his at all -> CHAT.

A question about how taxes, insurance, mortgages or medicine work in general
needs nothing of Sean's, so it is CHAT. The same question about HIS taxes, HIS
policy or HIS body needs his records, so it is JAX. The word "my" is the usual
tell.

You are not deciding whether anything is private. Nothing you can answer sends
a question to Venice, and a separate deterministic layer has already stopped
whatever looked sensitive before you were asked. If you are unsure between the
two, answer JAX: a general question sent to Jax costs a slower answer, and a
question about Sean answered by the small local model costs a wrong one.

Examples:
how far is Cabo da Roca from Lisbon -> CHAT
what's on my GitHub queue -> JAX
what did I spend on the Quinta last month -> JAX
what's the capital of Portugal -> CHAT
did that pull request pass CI -> JAX
how does health insurance work in Portugal -> CHAT
when is my next appointment -> JAX
tell me a joke -> CHAT

Now route this request. One word."""


# --- layer 0: conversational stickiness -----------------------------------
#
# The single biggest source of leaks, measured, is the elliptical follow-up:
# "did it come back clear", "what's left to pay on it", "is that sorted yet".
# In isolation those are unclassifiable - a human could not route them either.
# They are only answerable relative to the turn before, so the router gets the
# recent turns, and a private conversation stays private until it demonstrably
# changes subject.

_ANAPHOR = re.compile(
    r"\b(?:it|its|that|this|those|these|they|them|their|he|him|his|she|her|"
    r"hers|the same|the one|there|then|both|either)\b", re.I)
# A concrete noun of Sean's working life. Its presence means the utterance
# stands on its own and is not a continuation.
_TOPIC_SHIFT = re.compile(
    r"\b(?:github|pull request|pr|repo|branch|deploy|ci|server|container|site|"
    r"website|lead|leads|student|cohort|calendar|discord|email|briefing|blog|"
    r"post|tweet|oura|sleep|workout|weather|capital|joke|recipe|define|"
    r"what is a|what's a|who was|how do you say)\b", re.I)


def is_elliptical(text: str) -> bool:
    """True when the utterance cannot be understood without the turn before it."""
    words = text.split()
    if len(words) > 12:
        return False
    if _TOPIC_SHIFT.search(text):
        return False
    return bool(_ANAPHOR.search(text))


def _extract_route(raw: str) -> Route | None:
    """Pull a route word out of whatever the model actually said.

    Small models pad ("Route: JAX", "**CHAT**", "JAX."). Take the first route
    word that appears; if two appear, the first one is the answer.
    """
    # JILL is still accepted from the wire because an older prompt seated in
    # Ollama's cache can still emit it. It is mapped to HELD, not to a
    # destination, and _apply_default then folds it into JAX. Silently dropping
    # an unrecognised word would be worse: it would look like a parse failure.
    m = re.search(r"\b(CHAT|JAX|JILL|HELD)\b", raw.upper())
    return Route(m.group(1)) if m else None


def _with_history(text: str, history: list[dict] | None) -> str:
    """Render the classifier's user turn, recent context first.

    History goes inside the single user message rather than as real chat turns
    so the cached system prefix stays byte-identical and Ollama keeps the KV
    cache. Two turns is enough to resolve an anaphor and short enough not to
    drag an old subject into a new one.
    """
    if not history:
        return text
    lines = [f"{'Sean' if h['role'] == 'user' else 'You'}: {h['content']}"
             for h in history[-4:]]
    return "Recent conversation:\n" + "\n".join(lines) + f"\n\nRoute this: {text}"


async def classify_llm(
    text: str,
    client: httpx.AsyncClient,
    model: str = ROUTER_MODEL,
    timeout: float = ROUTER_TIMEOUT_SECS,
    history: list[dict] | None = None,
) -> tuple[Route | None, str]:
    """One classification call. Returns (route or None, raw model text)."""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": ROUTER_SYSTEM},
            {"role": "user", "content": _with_history(text, history)},
        ],
        "stream": False,
        # num_predict is 8, not 1: some models emit a leading space or a
        # "Route:" prefix, and truncating to one token turns a correct answer
        # into an unparseable one that falls through to JILL.
        "options": {"temperature": 0.0, "top_p": 1.0, "num_predict": 8, "seed": 0},
        # Ollama's default keep_alive is five minutes, so a gap in the
        # conversation evicts both voters and the next turn pays a cold load.
        # Measured live on 2026-08-07: seventeen minutes idle, then a classify
        # that normally takes 620 ms took 4002 ms, blew the 3 s gate and fell
        # closed to JILL. Sean asked a GitHub question and got Jill saying she
        # has no access to his GitHub. The router was not wrong, it never
        # answered. Boot-time warming does not help here - the eviction happens
        # mid-session.
        "keep_alive": KEEP_ALIVE,
    }
    r = await client.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=timeout)
    r.raise_for_status()
    raw = (r.json().get("message") or {}).get("content", "")
    return _extract_route(raw), raw


# --- layer 4: the second opinion, retired ---------------------------------
#
# Two models trained by different people fail on different utterances, so a
# union over their votes covered more of the space than either alone. That was
# worth 200-460 ms because the axis it covered was privacy: either voter saying
# JILL was enough to keep a question off the wire to Anthropic.
#
# With Venice out of the loop there is no privacy verdict to union over. The
# only disagreement left is CHAT versus JAX, where the second voter can pull an
# escalation down to a local answer - trading a correct answer for a fast wrong
# one. So it is off by default (ROUTER_MODEL_2) and the code below stays,
# because turning it back on is one environment variable and the reasoning here
# may not survive the next redesign.

async def _vote(text: str, client: httpx.AsyncClient, model: str, timeout: float,
                history: list[dict] | None) -> tuple[Route | None, str]:
    """One voter's answer, with any failure rendered as an abstention."""
    try:
        parsed, raw = await classify_llm(text, client, model=model, timeout=timeout,
                                         history=history)
    except Exception as e:  # noqa: BLE001 - an abstention biases the safe way
        return None, f"{model}=error:{type(e).__name__}"
    return parsed, f"{model}={parsed.value if parsed else 'unparsed:' + raw.strip()[:24]}"


async def classify_vote(
    text: str,
    client: httpx.AsyncClient,
    model: str = ROUTER_MODEL,
    model2: str | None = None,
    timeout: float = ROUTER_TIMEOUT_SECS,
    history: list[dict] | None = None,
) -> tuple[Route | None, str, dict[str, Route | None], bool]:
    """Classify, asking the second voter only when the answer would leave the box.

    Returns (route or None, raw text for the log, per-model votes, diverted).
    None means the primary produced no usable answer, which the caller turns
    into JILL. `diverted` is True when the second voter overruled a JAX.
    """
    primary, raw = await _vote(text, client, model, timeout, history)
    votes: dict[str, Route | None] = {model: primary}

    if primary is not Route.JAX or not model2 or model2 == model:
        return primary, raw, votes, False

    second, raw2 = await _vote(text, client, model2, timeout, history)
    votes[model2] = second
    joined = f"{raw} {raw2}"
    if second is None:
        # A voter that could not answer is a broken voter, not a verdict. With
        # only CHAT and JAX left there is no safe third place to put it, so the
        # primary stands and the failure shows up in the logs.
        return primary, joined, votes, False
    if second is Route.CHAT:
        return Route.CHAT, joined, votes, True
    return primary, joined, votes, False


def _apply_default(parsed: Route | None) -> Route:
    """Fold a classifier verdict into the two destinations that still exist.

    The classifier is only asked to separate CHAT from JAX now. If an older
    prompt or a confused model still emits a privacy verdict it is discarded
    rather than honoured: that judgement belongs to the deterministic keyword
    net, which has already run above, and to Sean, who is the actual boundary.

    A vote of None means the classifier gave no usable answer at all, which is a
    failure rather than a verdict, so it takes the default too.
    """
    if parsed is None:
        return DEFAULT_ROUTE
    if parsed is Route.HELD:
        return Route.JAX
    return parsed


async def route(
    text: str,
    client: httpx.AsyncClient,
    model: str = ROUTER_MODEL,
    prefilter: bool = PREFILTER_ENABLED,
    history: list[dict] | None = None,
    last_route: Route | None = None,
    sticky: bool = True,
    model2: str | None = "",
) -> Decision:
    """Full decision. Never raises - it escalates instead.

    Layers, in order: named override, hold stickiness, keyword net, classifier.
    The keyword net is the only layer that can refuse; everything else chooses
    between answering locally and escalating.

    `model2` defaults to the configured second voter; pass None or "off" to run
    the single-model router.
    """
    t0 = time.perf_counter()
    # "" is the sentinel for "whatever is configured"; None means nobody.
    model2 = second_model(ROUTER_MODEL_2 if model2 == "" else (model2 or ""))

    def done(r: Route, source: str, raw: str = "",
             votes: dict[str, Route | None] | None = None,
             ask: bool = False) -> Decision:
        return Decision(r, source, (time.perf_counter() - t0) * 1000,
                        raw, votes or {}, ask)

    if not text.strip():
        return done(Route.CHAT, "override")

    named = explicit_override(text)
    if named is not None:
        return done(named, "override")

    # A held thread keeps holding.
    #
    # This used to require the follow-up to be elliptical, which caught "did it
    # come back clear" and missed "what were the numbers they read out to me" -
    # the same private thread, one sentence later, phrased as a full question.
    # With an escalation on the other side of that miss it is a disclosure, so
    # holding now covers the whole thread rather than the elliptical part of it.
    #
    # The way out is Sean naming Jax, which is the same steering the rest of
    # this file gives him.
    if sticky and last_route is Route.HELD:
        if STICKY_WHOLE_THREAD or is_elliptical(text):
            return done(Route.HELD, "sticky")

    # The keyword net, and the only thing in this file that can refuse.
    #
    # It does not route. It cannot: HELD has no destination, so the worst it can
    # do is stop a turn Sean then repeats or takes to Jill himself. That is what
    # makes it trustworthy in a way the three-way classifier never was - a layer
    # whose only power is to refuse cannot leak.
    #
    # Trigger-happy on purpose. A false positive costs one turn.
    if prefilter and sensitive_prefilter(text):
        return done(Route.HELD, "prefilter")

    try:
        parsed, raw, votes, diverted = await classify_vote(
            text, client, model=model, model2=model2, history=history)
    except Exception as e:  # noqa: BLE001
        # A classifier that fell over tells us nothing about the content, and
        # the keyword net has already had its say above. Escalating is the
        # useful failure: the alternative is answering a real question with a 3B
        # model, and Sean has already decided what he is willing to say aloud.
        return done(DEFAULT_ROUTE, "fallback", f"error: {type(e).__name__}: {e}")

    if parsed is None or any(v is None for v in votes.values()):
        # Labelling it "fallback" keeps a broken voter visible in the logs and
        # out of the classifier latency stats.
        return done(_apply_default(parsed), "fallback", raw, votes)
    return done(_apply_default(parsed), "second-opinion" if diverted else "model",
                raw, votes)
