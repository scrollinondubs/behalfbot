#!/usr/bin/env python3
"""bfl-vision-extract.py — hybrid cloud vision extraction for Body for Life photos.

Routing:
    classify  → gpt-4o-mini (cheap, reliable single-word classifier)
    workout_log / meal_log / aerobic_log → Claude Opus (best handwriting accuracy)
    food_photo → gpt-4o-mini (good enough, ~20x cheaper than Opus)
    other → skip

Per-extraction: emits JSON to ${BFL_ARCHIVE_DIR}/extractions/<basename>.json
with cost + token counts + model used, and appends a line to
${BFL_ARCHIVE_DIR}/extractions/_costs.jsonl for running-total tracking.

Earlier versions used local qwen2.5vl / llava — those returned unusable
output on dense handwriting. Local-model path removed. This script is
cloud-only.

Usage:
    plugins/bfl/scripts/bfl-vision-extract.py --input-dir <dir>
    plugins/bfl/scripts/bfl-vision-extract.py --only IMG_8451 --only IMG_8452

Env vars:
    BFL_ARCHIVE_DIR   absolute path; default ~/behalfbot-archive/bfl
    OPENAI_API_KEY    required for the classifier + food-photo extractor
    (Claude calls route through `claude -p` and require the chassis user
     to be logged into Claude Code; no API key.)
"""
from __future__ import annotations
import argparse
import base64
import json
import os
import pathlib
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent


def archive_root() -> pathlib.Path:
    explicit = os.environ.get("BFL_ARCHIVE_DIR")
    if explicit:
        return pathlib.Path(explicit).expanduser()
    return pathlib.Path.home() / "behalfbot-archive" / "bfl"


# ───────── prompts ─────────

CLASSIFY_PROMPT = """Classify this image. Return ONE WORD:
- "workout_log" if it's a handwritten notebook page logging weight-training exercises (sets, reps, weights in a table)
- "aerobic_log" if it's a handwritten notebook page with a hand-drawn GRAPH showing exercise intensity over time (typically a 20-min run profile with 4 intensity peaks, axes labelled with minutes + intensity 0-10)
- "meal_log" if it's a handwritten notebook page logging meals
- "food_photo" if it's a photograph of actual food
- "other" for anything else

Respond with only the classification word, no other text."""

WORKOUT_PROMPT = """This is a handwritten workout log page from the Body for Life program. Extract the data into this exact JSON shape:
{
  "date": "YYYY-MM-DD or null if unclear",
  "workout_start": "HH:MM from upper-left corner of the page, or null",
  "day_type": "upper" or "lower" or "aerobic" or null,
  "muscle_groups_worked": ["chest", "triceps", ...],
  "exercises": [
    {
      "name": "Bench Press",
      "muscle_group": "chest",
      "is_main": true,
      "sets": [
        {"set": 1, "reps": 12, "weight_kg": 60, "intensity": 7, "up_arrow": false}
      ]
    }
  ],
  "notes": "anything notable visible"
}

Extraction rules:
- Weights are in KILOGRAMS. If the weight column shows "/" or "0" or is blank (bodyweight / machine with levels), use weight_kg: null.
- Machine level columns (e.g. Incline Slider Machine levels 1-10) go in weight_kg: null and should be mentioned in notes.
- A small up-arrow (↑) after a set means "make heavier next time" → up_arrow: true on that set, false otherwise.
- Column order per set row: reps | weight_kg | intensity (1-10, may be decimal like 8.5).
- Workouts typically structure as: 1 MAIN exercise for 5 sets (is_main: true), then a different exercise in the same muscle group for follow-up sets (is_main: false).
- Section headers label muscle groups: chest/back/shoulders/biceps/triceps (upper); quads/hamstrings/calves/abs (lower).
- Workout start time is usually in the UPPER-LEFT corner of the page.
- Date is typically in the upper-right, format like "4/19/26" = 2026-04-19.
- Track ACTUAL only, never planned.

Return ONLY valid JSON, no prose. If you can't read a field confidently, use null rather than guessing."""

MEAL_PROMPT = """This is a handwritten meal log page from the Body for Life Eating-for-Life Method. Extract into this exact JSON:
{
  "date": "YYYY-MM-DD or null",
  "meals": [
    {"meal_num": 1, "time_actual": "8:30 AM or null", "description": "what was eaten", "protein_portions": 1, "carb_portions": 1}
  ],
  "total_protein_portions": 6,
  "total_carb_portions": 6,
  "water_tick_marks": 5,
  "notes": "any notes"
}

Rules:
- Log ACTUAL meals only. Only emit time_actual, never time_planned.
- Water is tracked as tick marks at the bottom of the page. Count them precisely. The tally convention is 4 verticals + 1 diagonal crossbar = 5. Count carefully.
- Only include meals that have an actual handwritten entry.

Return ONLY valid JSON."""

AEROBIC_PROMPT = """This is a handwritten aerobic/cardio log page from the Body for Life program. The page contains a hand-drawn GRAPH where:
- The Y-AXIS is TIME in minutes (typically 0 at top to 20 at bottom, or 0 → 20 labelled).
- The X-AXIS is INTENSITY on a 0-10 scale.
- A line/curve traces the workout's intensity profile minute-by-minute.
- BFL calls for a 20-min workout with 4 peaks roughly at minutes 6, 10, 14, 19 (the 19-min peak being the hardest push).

Extract into this exact JSON:
{
  "date": "YYYY-MM-DD or null",
  "workout_start": "HH:MM from upper-left corner, or null",
  "activity_type": "run" or "cycling" or "rowing" or "stairmaster" or null,
  "total_minutes": 20,
  "actual_intensities_json": [3, 4, 5, 5, 6, 7, 5, 5, 7, 8, 5, 5, 8, 9, 5, 5, 8, 9, 10, 4],
  "notes": "anything worth flagging (what activity, effort notes, anecdotal commentary)"
}

Rules:
- `actual_intensities_json` MUST be an array of exactly N integers where N = total_minutes (typically 20). Each integer is the intensity 0-10 at that minute. Read the curve carefully and sample it minute-by-minute.
- If the graph is axes-only with no curve drawn, return an empty array and set notes="no curve drawn".
- If `total_minutes` is not 20 (e.g. 30-min workout), emit the correct length array.

Return ONLY valid JSON, no prose."""

FOOD_PHOTO_PROMPT = """This is a photograph of food. Identify the items ACTUALLY VISIBLE in the image. Return JSON:
{
  "items": ["grilled chicken breast", "brown rice", ...],
  "estimated_portion_grams": 450,
  "estimated_calories": 550,
  "estimated_protein_g": 45,
  "estimated_carbs_g": 50,
  "estimated_fat_g": 15,
  "confidence": 0.7,
  "notes": "anything worth flagging"
}

Rules:
- Report ONLY items that are actually visible. Do NOT hallucinate a generic meal.
- If branded containers are visible, include the brand in the item name.
- If the image shows leftovers or an empty plate, say so in notes and set confidence low.

Return ONLY valid JSON."""

# ───────── model configs ─────────

CLASSIFIER_MODEL = "gpt-4o-mini"
WORKOUT_MODEL = "claude-opus-4-7"
AEROBIC_MODEL = "claude-opus-4-7"   # Reading a hand-drawn graph needs the best vision
MEAL_MODEL = "claude-opus-4-7"
FOOD_MODEL = "gpt-4o-mini"

# Pricing in USD per 1M tokens. Update when providers change prices.
PRICING = {
    "claude-opus-4-7":   {"input": 15.0, "output": 75.0},
    "claude-sonnet-4-6": {"input": 3.0,  "output": 15.0},
    "gpt-4o-mini":       {"input": 0.15, "output": 0.60},
}


def cost_of(model: str, tokens_in: int, tokens_out: int) -> float:
    p = PRICING[model]
    return (tokens_in * p["input"] + tokens_out * p["output"]) / 1_000_000


# ───────── model callers ─────────

def call_claude(model: str, prompt: str, img_b64: str, api_key: str | None = None) -> tuple[str, int, int]:
    """Vision call routed via the local `claude -p` CLI so it bills against
    the installer's Claude Code subscription, NOT against PAYG API credits.

    Pre-fix history: this function POST'd directly to api.anthropic.com,
    which bills PAYG via the Anthropic API + auto-recharges the card.
    Conservation mode + the per-block raw-token brake are blind to that
    path because they only see `claude -p` invocations.

    Post-fix: write the image to a temp file, invoke `claude -p` with
    --output-format json + --model <opus|sonnet|haiku>, parse the result.

    `api_key` arg kept for signature compatibility (call sites pass it)
    but unused — `claude -p` reads OAuth credentials from the installer's
    logged-in Claude Code session, no API key needed.
    """
    import base64
    import subprocess
    import tempfile

    short_alias = {
        "claude-opus-4-7": "opus",
        "claude-opus-4-6": "opus",
        "claude-sonnet-4-6": "sonnet",
        "claude-haiku-4-5-20251001": "haiku",
        "claude-haiku-4-5": "haiku",
    }.get(model, model)

    img_bytes = base64.b64decode(img_b64)
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
        tf.write(img_bytes)
        img_path = tf.name

    try:
        cli_prompt = (
            f"Read the image at {img_path} (use the Read tool). Then complete "
            f"the following task based ONLY on what you see in that image. "
            f"Return your answer as the final text in your reply, no preamble, "
            f"no commentary outside the structured answer.\n\n"
            f"---\n\n{prompt}"
        )

        cmd = [
            "claude", "-p",
            "--model", short_alias,
            "--output-format", "json",
            "--permission-mode", "bypassPermissions",
            cli_prompt,
        ]
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=240,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"claude -p exited {result.returncode}: stderr={result.stderr[:500]}"
            )
        envelope = json.loads(result.stdout)
        text = envelope.get("result") or ""
        u = envelope.get("usage") or {}
        in_tokens = int(u.get("input_tokens") or 0)
        out_tokens = int(u.get("output_tokens") or 0)
        return text, in_tokens, out_tokens
    finally:
        try:
            os.unlink(img_path)
        except OSError:
            pass


def call_openai(model: str, prompt: str, img_b64: str, api_key: str) -> tuple[str, int, int]:
    body = {
        "model": model,
        "temperature": 0.1,
        "max_tokens": 4096,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
            ],
        }],
    }
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {api_key}", "content-type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=240) as r:
        data = json.loads(r.read())
    text = data["choices"][0]["message"]["content"]
    u = data.get("usage", {})
    return text, u.get("prompt_tokens", 0), u.get("completion_tokens", 0)


def call_model(model: str, prompt: str, img_b64: str) -> tuple[str, int, int]:
    if model.startswith("claude-"):
        return call_claude(model, prompt, img_b64, api_key=None)
    if model.startswith("gpt-"):
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY not set")
        return call_openai(model, prompt, img_b64, key)
    raise RuntimeError(f"Unknown model family: {model}")


# ───────── helpers ─────────

def parse_json_ish(text: str) -> dict | None:
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1]) if lines[-1].startswith("```") else "\n".join(lines[1:])
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(text[start:end + 1])
    except Exception:
        return None


def normalize_classification(text: str) -> str:
    t = text.strip().lower().split()[0] if text.strip() else "other"
    for key in ("workout_log", "aerobic_log", "meal_log", "food_photo"):
        if key in t:
            return key
    return "other"


# ───────── extraction ─────────

CAPTION_HINT_TEMPLATE = (
    '\nUser caption (provided by the person who took the photo): "{caption}"\n\n'
    'Use this caption as a tiebreaker when visual identification is ambiguous '
    "(lighting, occlusion, cut shape). Do NOT use the caption to override items "
    "that are clearly visible in the photo - if the caption says \"salmon\" but the "
    "photo unambiguously shows pasta, report pasta. If the caption is vague or "
    "unrelated to food, ignore it."
)


def _read_caption(img_path: pathlib.Path) -> str | None:
    """Read the sibling .caption.txt for img_path. Returns stripped text
    (capped at 500 chars) or None if absent or empty. Brings <v1-reference-install> PR #550 upstream."""
    caption_path = img_path.with_suffix(".caption.txt")
    if not caption_path.exists():
        return None
    text = caption_path.read_text(encoding="utf-8", errors="replace").strip()[:500]
    return text if text else None


def extract_one(img_path: pathlib.Path, out_dir: pathlib.Path, cost_log: pathlib.Path) -> dict:
    b64 = base64.b64encode(img_path.read_bytes()).decode()
    total_cost = 0.0
    token_steps = []

    # Read optional caption hint once up-front. Only food_photo and meal_log
    # prompts receive it - workout/aerobic prompts are handwritten log pages
    # where vision misclassification of food items isn't a failure mode.
    # Brings <v1-reference-install> PR #550 upstream.
    caption = _read_caption(img_path)
    if caption:
        print(f"[vision] using caption hint for {img_path.name}: {caption[:80]!r}", file=sys.stderr)

    # Step 1: classify — gpt-4o-mini first (cheap). If it returns "other",
    # retry with Opus (more reliable on rotated pages + hand-drawn graphs).
    t0 = time.time()
    cls_text, in_tok, out_tok = call_model(CLASSIFIER_MODEL, CLASSIFY_PROMPT, b64)
    cls_cost = cost_of(CLASSIFIER_MODEL, in_tok, out_tok)
    total_cost += cls_cost
    token_steps.append({"step": "classify", "model": CLASSIFIER_MODEL, "in": in_tok, "out": out_tok, "cost": cls_cost})
    classification = normalize_classification(cls_text)
    if classification == "other":
        # Escalate to Opus — aerobic_log pages rotated 90° often confuse 4o-mini
        cls2_text, in2, out2 = call_model("claude-opus-4-7", CLASSIFY_PROMPT, b64)
        cls2_cost = cost_of("claude-opus-4-7", in2, out2)
        total_cost += cls2_cost
        token_steps.append({"step": "classify_retry", "model": "claude-opus-4-7", "in": in2, "out": out2, "cost": cls2_cost})
        classification = normalize_classification(cls2_text)

    # Step 2: route + extract. Append caption hint to food_photo and meal_log
    # prompts when a sibling .caption.txt is present (<v1-reference-install> PR #550).
    def _with_caption(base_prompt: str) -> str:
        if not caption:
            return base_prompt
        return base_prompt + CAPTION_HINT_TEMPLATE.format(caption=caption)

    route = {"workout_log": (WORKOUT_MODEL, WORKOUT_PROMPT),
             "aerobic_log": (AEROBIC_MODEL, AEROBIC_PROMPT),
             "meal_log": (MEAL_MODEL, _with_caption(MEAL_PROMPT)),
             "food_photo": (FOOD_MODEL, _with_caption(FOOD_PHOTO_PROMPT))}.get(classification)
    extraction = None
    raw_text = None
    extract_model = None
    if route:
        extract_model, prompt = route
        raw_text, e_in, e_out = call_model(extract_model, prompt, b64)
        e_cost = cost_of(extract_model, e_in, e_out)
        total_cost += e_cost
        token_steps.append({"step": "extract", "model": extract_model, "in": e_in, "out": e_out, "cost": e_cost})
        extraction = parse_json_ish(raw_text)

    elapsed = time.time() - t0
    result = {
        "source": str(img_path),
        "classification": classification,
        "extraction": extraction,
        "raw_response_preview": (raw_text[:1500] if raw_text else None),
        "elapsed_s": round(elapsed, 1),
        "extract_model": extract_model,
        "cost_usd": round(total_cost, 5),
        "token_steps": token_steps,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    out_path = out_dir / (img_path.stem + ".json")
    out_path.write_text(json.dumps(result, indent=2))

    with cost_log.open("a") as f:
        f.write(json.dumps({
            "timestamp": result["timestamp"],
            "image": img_path.stem,
            "classification": classification,
            "extract_model": extract_model,
            "cost_usd": round(total_cost, 5),
            "tokens_in":  sum(s["in"]  for s in token_steps),
            "tokens_out": sum(s["out"] for s in token_steps),
        }) + "\n")

    return result


def main() -> int:
    root = archive_root()
    ap = argparse.ArgumentParser()
    ap.add_argument("--input-dir", default=str(root / "raw"))
    ap.add_argument("--out-dir", default=str(root / "extractions"))
    ap.add_argument("--cost-log", default=str(root / "extractions" / "_costs.jsonl"))
    ap.add_argument("--force", action="store_true", help="re-extract even if JSON already exists")
    ap.add_argument("--only", action="append", default=[], help="only process these basenames (repeatable)")
    args = ap.parse_args()

    in_dir = pathlib.Path(args.input_dir)
    out_dir = pathlib.Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cost_log = pathlib.Path(args.cost_log)
    cost_log.parent.mkdir(parents=True, exist_ok=True)

    images = sorted(list(in_dir.glob("*.jpg")) + list(in_dir.glob("*.jpeg")) + list(in_dir.glob("*.png")))
    if not args.force:
        before = len(images)
        images = [p for p in images if not (out_dir / (p.stem + ".json")).exists()]
        skipped = before - len(images)
        if skipped:
            print(f"Skipping {skipped} images with existing extractions (use --force to override)")
    if args.only:
        only = set(args.only)
        images = [p for p in images if p.stem in only]

    if not images:
        print("No images to process.")
        return 0

    print(f"Processing {len(images)} images. Classifier={CLASSIFIER_MODEL}, workout/meal={WORKOUT_MODEL}, food={FOOD_MODEL}")

    session_cost = 0.0
    results = []
    for i, p in enumerate(images, 1):
        print(f"\n[{i}/{len(images)}] {p.name} ...", flush=True)
        try:
            r = extract_one(p, out_dir, cost_log)
        except Exception as e:
            print(f"  ERROR: {e}")
            continue
        session_cost += r["cost_usd"]
        results.append(r)
        print(f"  classification: {r['classification']}")
        if r["extract_model"]:
            print(f"  extracted via {r['extract_model']} in {r['elapsed_s']}s, cost=${r['cost_usd']:.4f}")
        else:
            print(f"  skipped extraction (classification={r['classification']})")

    print("\n" + "=" * 60)
    print(f"SESSION TOTAL: ${session_cost:.4f} across {len(results)} image(s)")
    print("=" * 60)
    for r in results:
        src = pathlib.Path(r.get("source", "?")).name
        print(f"  {src:20} {r['classification']:12} model={r.get('extract_model','-'):20} ${r['cost_usd']:.4f}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
