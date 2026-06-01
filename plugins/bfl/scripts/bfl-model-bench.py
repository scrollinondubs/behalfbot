#!/usr/bin/env python3
"""bfl-model-bench.py — compare Claude Sonnet vs Opus vs GPT-4o-mini on BFL photos.

Runs each model against each image in ${BFL_ARCHIVE_DIR}/raw/ using the
canonical extraction prompts from bfl-vision-extract.py. Scores results
against human-verified ground truth in ${BFL_ARCHIVE_DIR}/ground-truth/.

Usage:
    plugins/bfl/scripts/bfl-model-bench.py [--images IMG_8451 IMG_8452] [--models claude-sonnet-4-6 claude-opus-4-7 gpt-4o-mini]

Env vars:
    BFL_ARCHIVE_DIR     absolute path; default ~/behalfbot-archive/bfl
    ANTHROPIC_API_KEY   for Claude (this script bypasses the `claude -p` path
                         on purpose — benchmark accuracy needs deterministic
                         API calls without the CLI envelope's extra prompting)
    OPENAI_API_KEY      for GPT
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


def archive_root() -> pathlib.Path:
    explicit = os.environ.get("BFL_ARCHIVE_DIR")
    if explicit:
        return pathlib.Path(explicit).expanduser()
    return pathlib.Path.home() / "behalfbot-archive" / "bfl"


CLASSIFY_PROMPT = """Classify this image. Return ONE WORD:
- "workout_log" if it's a handwritten notebook page logging weight-training exercises
- "meal_log" if it's a handwritten notebook page logging meals
- "food_photo" if it's a photograph of actual food
- "other" otherwise

Respond with only the classification word."""

WORKOUT_PROMPT = """This is a handwritten workout log page from the Body for Life program. Extract into this exact JSON shape:
{
  "date": "YYYY-MM-DD or null",
  "workout_start": "HH:MM from upper-left corner, or null",
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
  "notes": "anything notable"
}

Rules:
- Weights in kg. If column is "/" or "0" (bodyweight machine), use weight_kg: null.
- ↑ (up-arrow) after a set means "heavier next time" → up_arrow: true for that set.
- Columns per set row: reps | weight_kg | intensity (1-10).
- Sections: chest/back/shoulders/biceps/triceps (upper); quads/hamstrings/calves/abs (lower).
- Main exercise = 5 sets on one movement (is_main: true), then switch.
- Workout start time is in the UPPER-LEFT corner of the page.

Return ONLY valid JSON, no prose."""

MEAL_PROMPT = """This is a handwritten meal log page from Body for Life. Extract into this exact JSON:
{
  "date": "YYYY-MM-DD or null",
  "meals": [
    {"meal_num": 1, "time_actual": "8:30 AM or null", "description": "what was eaten"}
  ],
  "water_tick_marks": 5,
  "notes": "any notes"
}

Rules:
- Actual meal times only — not planned.
- Water tracked as tick marks at the bottom of page. Count them precisely. Each tick ≈ 12oz glass. The common "tally" convention is 4 verticals + 1 diagonal = 5.
- Meal num and time are usually at the start of the line.

Return ONLY valid JSON."""

FOOD_PHOTO_PROMPT = """This is a photograph of food. Identify the actual items visible. Return JSON:
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
- Report ONLY items actually visible in this image. Do not hallucinate a generic meal.
- If branded containers are visible, include the brand in the item name.

Return ONLY valid JSON."""


def read_image_b64(img_path: pathlib.Path) -> str:
    return base64.b64encode(img_path.read_bytes()).decode()


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


# ───────── model callers ─────────

def call_claude(model: str, prompt: str, img_b64: str, api_key: str) -> tuple[str, int, int]:
    body = {
        "model": model,
        "max_tokens": 4096,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64}},
                {"type": "text", "text": prompt},
            ],
        }],
    }
    if "opus" not in model:
        body["temperature"] = 0.1
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode(),
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        data = json.loads(r.read())
    text = data["content"][0]["text"]
    usage = data.get("usage", {})
    return text, usage.get("input_tokens", 0), usage.get("output_tokens", 0)


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
        headers={
            "Authorization": f"Bearer {api_key}",
            "content-type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        data = json.loads(r.read())
    text = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})
    return text, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)


PRICING = {
    "claude-sonnet-4-6": {"input": 3.0,  "output": 15.0, "caller": "claude"},
    "claude-opus-4-7":   {"input": 15.0, "output": 75.0, "caller": "claude"},
    "gpt-4o-mini":       {"input": 0.15, "output": 0.60, "caller": "openai"},
}


# ───────── scoring ─────────

def score_workout(gt: dict, actual: dict) -> dict:
    gt_sets = [(e["name"].lower(), s["set"], s.get("reps"), s.get("weight_kg"), s.get("intensity"))
               for e in gt["extraction"]["exercises"] for s in e["sets"]]
    ac_sets = [(e.get("name", "").lower(), s.get("set"), s.get("reps"), s.get("weight_kg"), s.get("intensity"))
               for e in (actual.get("exercises") or []) for s in (e.get("sets") or [])]

    gt_ex_names = {e["name"].lower() for e in gt["extraction"]["exercises"]}
    ac_ex_names = {e.get("name", "").lower() for e in (actual.get("exercises") or [])}
    name_overlap = gt_ex_names & ac_ex_names

    def key(t): return (t[0], t[1])
    gt_map = {key(t): t for t in gt_sets}
    ac_map = {key(t): t for t in ac_sets}
    precise_matches = 0
    for k, v in gt_map.items():
        if k in ac_map:
            _, _, g_reps, g_kg, g_int = v
            _, _, a_reps, a_kg, a_int = ac_map[k]
            if (g_reps == a_reps
                and (g_kg is None and a_kg is None
                     or (g_kg is not None and a_kg is not None and abs(float(g_kg) - float(a_kg)) <= 1))
                and (g_int == a_int if isinstance(g_int, int) and isinstance(a_int, int)
                     else abs(float(g_int or 0) - float(a_int or 0)) < 0.75)):
                precise_matches += 1

    return {
        "gt_exercises": len(gt["extraction"]["exercises"]),
        "ac_exercises": len(actual.get("exercises") or []),
        "exercise_name_overlap": len(name_overlap),
        "gt_sets": len(gt_sets),
        "ac_sets": len(ac_sets),
        "precise_set_matches": precise_matches,
        "date_match": gt["extraction"].get("date") == actual.get("date"),
        "start_match": gt["extraction"].get("workout_start") == actual.get("workout_start"),
    }


def score_meal(gt: dict, actual: dict) -> dict:
    gt_meals = gt["extraction"]["meals"]
    ac_meals = actual.get("meals") or []
    num_match = 0
    desc_overlap = 0
    for gm in gt_meals:
        for am in ac_meals:
            if am.get("meal_num") == gm["meal_num"]:
                num_match += 1
                if gm["description"].lower() in (am.get("description") or "").lower():
                    desc_overlap += 1
                break
    return {
        "gt_meals": len(gt_meals),
        "ac_meals": len(ac_meals),
        "num_match": num_match,
        "description_overlap": desc_overlap,
        "water_match": gt["extraction"].get("water_tick_marks") == actual.get("water_tick_marks"),
    }


def score_food_photo(gt: dict, actual: dict) -> dict:
    gt_items = [i.lower() for i in gt["extraction"]["items"]]
    ac_items = [i.lower() for i in (actual.get("items") or [])]
    matched = 0
    for gi in gt_items:
        gi_head = gi.split()[0]
        for ai in ac_items:
            if gi_head in ai or ai.split()[0] in gi:
                matched += 1
                break
    return {
        "gt_items": len(gt_items),
        "ac_items": len(ac_items),
        "matched_items": matched,
    }


# ───────── runner ─────────

def main() -> int:
    root = archive_root()
    raw_dir = root / "raw"
    gt_dir = root / "ground-truth"
    bench_dir = root / "benchmark"

    ap = argparse.ArgumentParser()
    ap.add_argument("--images", nargs="+", required=True, help="basenames (without extension) of images to bench")
    ap.add_argument("--models", nargs="+", default=list(PRICING.keys()))
    args = ap.parse_args()

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    openai_key = os.environ.get("OPENAI_API_KEY")

    summary = []

    for basename in args.images:
        img_path = raw_dir / (basename + ".jpg")
        if not img_path.exists():
            print(f"MISSING: {img_path}")
            continue
        gt_path = gt_dir / (basename + ".json")
        if not gt_path.exists():
            print(f"NO GROUND TRUTH: {gt_path}")
            continue
        gt = json.loads(gt_path.read_text())
        classification = gt["classification"]
        prompt = {"workout_log": WORKOUT_PROMPT, "meal_log": MEAL_PROMPT, "food_photo": FOOD_PHOTO_PROMPT}[classification]
        img_b64 = read_image_b64(img_path)

        for model in args.models:
            cfg = PRICING[model]
            out_dir = bench_dir / model
            out_dir.mkdir(parents=True, exist_ok=True)
            out_file = out_dir / (basename + ".json")

            print(f"\n=== {model} / {basename} ({classification}) ===", flush=True)
            t0 = time.time()
            try:
                if cfg["caller"] == "claude":
                    text, in_tok, out_tok = call_claude(model, prompt, img_b64, anthropic_key)
                else:
                    text, in_tok, out_tok = call_openai(model, prompt, img_b64, openai_key)
            except urllib.error.HTTPError as e:
                body = e.read().decode()[:400]
                print(f"  HTTP {e.code}: {body}")
                continue
            except Exception as e:
                print(f"  ERROR: {e}")
                continue

            elapsed = time.time() - t0
            cost = (in_tok * cfg["input"] + out_tok * cfg["output"]) / 1_000_000
            parsed = parse_json_ish(text)

            record = {
                "model": model,
                "image": basename,
                "classification": classification,
                "elapsed_s": round(elapsed, 1),
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "cost_usd": round(cost, 5),
                "raw_text": text[:2000],
                "parsed": parsed,
            }

            if parsed:
                if classification == "workout_log":
                    record["score"] = score_workout(gt, parsed)
                elif classification == "meal_log":
                    record["score"] = score_meal(gt, parsed)
                else:
                    record["score"] = score_food_photo(gt, parsed)
            else:
                record["score"] = {"parse_failed": True}

            out_file.write_text(json.dumps(record, indent=2))
            summary.append(record)
            print(f"  {elapsed:.1f}s | in={in_tok} out={out_tok} cost=${cost:.4f}")
            print(f"  score: {record['score']}")

    print("\n\n========== SUMMARY ==========")
    print(f"{'model':25} {'image':12} {'cost':>8} {'time':>7}  score")
    print("-" * 90)
    for r in summary:
        print(f"{r['model']:25} {r['image']:12} ${r['cost_usd']:.4f}  {r['elapsed_s']:.1f}s  {r['score']}")

    totals = {}
    for r in summary:
        totals.setdefault(r["model"], {"cost": 0, "time": 0, "n": 0})
        totals[r["model"]]["cost"] += r["cost_usd"]
        totals[r["model"]]["time"] += r["elapsed_s"]
        totals[r["model"]]["n"] += 1
    print("\n========== MODEL TOTALS ==========")
    for model, t in totals.items():
        avg = t["cost"] / max(t["n"], 1)
        print(f"{model:25}  n={t['n']}  total=${t['cost']:.4f}  avg=${avg:.4f}/photo  total_time={t['time']:.0f}s")

    return 0


if __name__ == "__main__":
    sys.exit(main())
