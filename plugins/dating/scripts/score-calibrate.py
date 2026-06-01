#!/usr/bin/env python3
"""score-calibrate.py - empirical SCORE_MIN/SCORE_MAX baseline tool for score-face.py.

Runs the scorer pipeline against a batch of images and prints the raw-score
distribution as a TSV. Use to re-baseline SCORE_MIN / SCORE_MAX in score-face.py
whenever the distribution drifts:
  - after a large batch addition to data/dating/taste-refs/positive/ via RHL
  - after a W_TEXT / W_IMAGE retune
  - after the CLIP model is upgraded

Ported from <v1-reference-install> PR #535. Chassis adaptations: CHASSIS_HOME env var used;
score-face.py loaded relative to this file's location.

Usage:
    ${CHASSIS_HOME}/.venv-clip/bin/python plugins/dating/scripts/score-calibrate.py PATH...
    # Score all positive refs:
    ${CHASSIS_HOME}/.venv-clip/bin/python plugins/dating/scripts/score-calibrate.py \
        ${CHASSIS_HOME}/data/dating/taste-refs/positive/*.{png,jpeg,jpg}

Output columns: path raw text_d img_top3 score verdict (TSV)

Set SCORE_MIN slightly below the lowest raw observed in the positive set;
set SCORE_MAX slightly above the highest. That maps the real distribution
to the 0-100 range without ceiling-pegging or floor-pegging.
"""
from __future__ import annotations

import importlib.util as _u
import os
import sys
from pathlib import Path

import numpy as np
import torch
import open_clip
from insightface.app import FaceAnalysis

CHASSIS_HOME = Path(os.environ.get("CHASSIS_HOME", Path(__file__).resolve().parent.parent.parent.parent))
PLUGIN_SCRIPTS = Path(__file__).resolve().parent

_spec = _u.spec_from_file_location("score_face", PLUGIN_SCRIPTS / "score-face.py")
_sf = _u.module_from_spec(_spec)
_spec.loader.exec_module(_sf)

REFS_ROOT = CHASSIS_HOME / "data" / "dating" / "taste-refs"


def main():
    paths = [Path(p) for p in sys.argv[1:]]
    if not paths:
        print("usage: score-calibrate.py PATH...", file=sys.stderr)
        return 1

    device = _sf.pick_device()
    print(f"# device: {device}", file=sys.stderr)

    model, _, preprocess = open_clip.create_model_and_transforms(
        _sf.CLIP_MODEL_NAME, pretrained=_sf.CLIP_PRETRAINED, device=device,
    )
    model.eval()
    tokenizer = open_clip.get_tokenizer(_sf.CLIP_MODEL_NAME)

    detector = FaceAnalysis(
        name="buffalo_l", providers=["CPUExecutionProvider"],
        allowed_modules=["detection"],
    )
    detector.prepare(ctx_id=0, det_size=(640, 640))

    taste_pos = np.load(REFS_ROOT / "taste_pos.npy")
    taste_neg_path = REFS_ROOT / "taste_neg.npy"
    taste_neg = np.load(taste_neg_path) if taste_neg_path.exists() else None

    print("# path\traw\ttext_d\timg_top3\tscore\tverdict")
    for p in paths:
        try:
            res = _sf.score_image(
                p, taste_pos, taste_neg,
                model, preprocess, tokenizer, detector, device,
            )
            print(f"{p.name}\t{res['raw']:.4f}\t{res['text_delta']:+.4f}\t{res['img_top3']:.4f}\t{res['score']}\t{res['verdict']}")
        except Exception as e:
            print(f"# ERROR {p}: {e}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main() or 0)
