#!/usr/bin/env python3
"""score-face.py - score a profile photo against the installer's taste using CLIP.

Scores using:
  1. Text-conditional SigLIP2 similarity against positive + negative prompts
     (hair color, aesthetic, vibe).
  2. Per-image max/top-3 cosine similarity against the taste_pos reference
     stack (built from vision board + past likes via taste-calibrate.py).

The final score is a weighted combination of (text_delta, image_top3).

Ported from <v1-reference-install> scripts/score-face.py (<v1-reference-install> PR #534 + #535). Chassis
adaptations vs <v1-reference-install> source:
  - REFS_ROOT resolved via CHASSIS_HOME env var
  - POS_PROMPTS/NEG_PROMPTS left as installer-tunable defaults; installer
    should override in installer-facts.md or a local wrapper if needed

Usage:
    source ${CHASSIS_HOME}/.venv-clip/bin/activate
    python plugins/dating/scripts/score-face.py <image_path> [--json]
    python plugins/dating/scripts/score-face.py <image_path> --refs /path/to/taste-refs/
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np
import torch
from PIL import Image

import open_clip
from insightface.app import FaceAnalysis

CHASSIS_HOME = Path(os.environ.get("CHASSIS_HOME", Path(__file__).resolve().parent.parent.parent.parent))
REFS_ROOT = CHASSIS_HOME / "data" / "dating" / "taste-refs"

CLIP_MODEL_NAME = "ViT-L-16-SigLIP2-384"
CLIP_PRETRAINED = "webli"

# Text prompts encoding the installer's type. These are placeholder
# defaults only - every installer replaces them with prompts encoding
# their own preferences (see installer-facts.md guidance).
POS_PROMPTS = [
    "a photo of a blonde woman with light hair",
    "a portrait of a blonde woman smiling warmly",
    "a natural girl-next-door with blonde or light brown hair",
    "a photo of a woman with warm blonde hair and a bright smile",
    "a classic feminine woman with light colored hair and natural look",
]
NEG_PROMPTS = [
    "a photo of a woman with dark brunette hair",
    "a portrait of a brunette woman with long dark hair",
    "a woman with edgy alternative fashion and dyed hair",
    "a high-fashion model with heavy makeup and dark features",
    "a photo of a woman with silver or platinum dyed hair",
]

# Weighting for the final combined score. Placeholder code defaults -
# they carry no meaning until calibrated against your own taste-refs
# stack and RHL feedback. Re-run score-calibrate.py after building your
# ref stack and whenever scoring quality drifts after a ref stack change.
W_TEXT = 1.5   # text_delta - moderating signal on hair/aesthetic
W_IMAGE = 1.0  # image_top3 - primary signal vs retrained positive stack
W_NEG = 0.3    # negative-ref subtraction weight

# Score rescaling bounds. Placeholder code defaults - run score-calibrate.py
# against your own positive ref stack to baseline them for your install, and
# re-run when the distribution drifts (after large positive-ref batch
# additions or W_TEXT/W_IMAGE retuning).
SCORE_MIN = 0.65  # combined-score lower bound (rescaled to 0)
SCORE_MAX = 1.05  # combined-score upper bound (rescaled to 100)

LIKE_THRESHOLD = 50
BORDERLINE_THRESHOLD = 40


def pick_device() -> torch.device:
    if torch.backends.mps.is_available():
        return torch.device("mps")
    if torch.cuda.is_available():
        return torch.device("cuda")
    return torch.device("cpu")


def crop_largest_face(img: Image.Image, detector):
    arr = np.asarray(img.convert("RGB"))[:, :, ::-1]
    faces = detector.get(arr)
    if not faces:
        return None
    faces.sort(key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]), reverse=True)
    x1, y1, x2, y2 = faces[0].bbox.astype(int)
    w, h = x2 - x1, y2 - y1
    mx, my = int(w * 0.4), int(h * 0.4)
    return img.crop((max(0, x1 - mx), max(0, y1 - my), min(img.width, x2 + mx), min(img.height, y2 + my)))


def verdict_from_score(score: float) -> str:
    if score >= LIKE_THRESHOLD:
        return "like"
    if score >= BORDERLINE_THRESHOLD:
        return "borderline"
    return "pass"


def score_image(
    image_path: Path,
    taste_pos: np.ndarray,
    taste_neg: np.ndarray | None,
    model,
    preprocess,
    tokenizer,
    detector,
    device: torch.device,
):
    img = Image.open(image_path)
    cropped = crop_largest_face(img, detector)
    face_detected = cropped is not None
    if cropped is None:
        cropped = img

    tensor = preprocess(cropped.convert("RGB")).unsqueeze(0).to(device)
    with torch.no_grad():
        img_feats = model.encode_image(tensor)
        img_feats = img_feats / img_feats.norm(dim=-1, keepdim=True)
        emb = img_feats.squeeze(0).float().cpu().numpy()

        pos_tokens = tokenizer(POS_PROMPTS).to(device)
        neg_tokens = tokenizer(NEG_PROMPTS).to(device)
        pos_txt = model.encode_text(pos_tokens)
        pos_txt = pos_txt / pos_txt.norm(dim=-1, keepdim=True)
        neg_txt = model.encode_text(neg_tokens)
        neg_txt = neg_txt / neg_txt.norm(dim=-1, keepdim=True)
        pos_txt = pos_txt.float().cpu().numpy()
        neg_txt = neg_txt.float().cpu().numpy()

    pos_txt_cos = float(np.dot(pos_txt, emb).mean())
    neg_txt_cos = float(np.dot(neg_txt, emb).mean())
    text_delta = pos_txt_cos - neg_txt_cos

    img_sims = sorted([float(np.dot(emb, r)) for r in taste_pos], reverse=True)
    img_top3 = sum(img_sims[:3]) / 3

    if taste_neg is not None and len(taste_neg) > 0:
        neg_sims = sorted([float(np.dot(emb, r)) for r in taste_neg], reverse=True)
        neg_top3 = sum(neg_sims[:3]) / 3
        img_score = img_top3 - W_NEG * neg_top3
    else:
        neg_top3 = None
        img_score = img_top3

    raw = W_TEXT * text_delta + W_IMAGE * img_score
    score_pct = max(0.0, min(100.0, (raw - SCORE_MIN) / (SCORE_MAX - SCORE_MIN) * 100.0))

    return {
        "score": round(score_pct, 1),
        "raw": round(raw, 4),
        "text_delta": round(text_delta, 4),
        "pos_txt_cos": round(pos_txt_cos, 4),
        "neg_txt_cos": round(neg_txt_cos, 4),
        "img_top3": round(img_top3, 4),
        "img_neg_top3": round(neg_top3, 4) if neg_top3 is not None else None,
        "face_detected": face_detected,
        "verdict": verdict_from_score(score_pct),
        "image_path": str(image_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Score a face image against taste refs")
    parser.add_argument("image_paths", nargs="+", type=Path, help="One or more face image paths")
    parser.add_argument("--json", action="store_true", help="Output JSON (one array)")
    parser.add_argument(
        "--refs",
        type=Path,
        default=REFS_ROOT,
        help=f"Directory containing taste_pos.npy [default: {REFS_ROOT}]",
    )
    args = parser.parse_args()

    pos_path = args.refs / "taste_pos.npy"
    if not pos_path.exists():
        print("taste_pos.npy not found - run taste-calibrate.py first", file=sys.stderr)
        return 1

    taste_pos = np.load(pos_path)
    if taste_pos.ndim == 1:
        taste_pos = taste_pos[None, :]

    neg_path = args.refs / "taste_neg.npy"
    taste_neg = None
    if neg_path.exists():
        taste_neg = np.load(neg_path)
        if taste_neg.ndim == 1:
            taste_neg = taste_neg[None, :]

    device = pick_device()
    model, _, preprocess = open_clip.create_model_and_transforms(
        CLIP_MODEL_NAME, pretrained=CLIP_PRETRAINED, device=device
    )
    model.eval()
    tokenizer = open_clip.get_tokenizer(CLIP_MODEL_NAME)

    detector = FaceAnalysis(
        name="buffalo_l",
        allowed_modules=["detection"],
        providers=["CPUExecutionProvider"],
    )
    detector.prepare(ctx_id=0, det_size=(640, 640))

    results = []
    for path in args.image_paths:
        if not path.exists():
            print(f"image not found: {path}", file=sys.stderr)
            continue
        r = score_image(path, taste_pos, taste_neg, model, preprocess, tokenizer, detector, device)
        results.append(r)

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            print(
                f"score={r['score']:5.1f}  verdict={r['verdict']:10}  "
                f"txt_delta={r['text_delta']:+.4f}  img_top3={r['img_top3']:.3f}  "
                f"face={r['face_detected']}  {r['image_path']}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
