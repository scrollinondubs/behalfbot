#!/usr/bin/env python3
"""dating-reconcile.py - close the RHL loop for the dating swipe pipeline.

Reads the installer's hand-sorted picks under the RHL_PICKS_DIR (see Path
constants below), computes accuracy vs the subagent's prior swipes, copies
false-negatives into the CLIP positive-ref stack, archives processed images,
rebuilds `data/dating/taste-refs/taste_pos.npy`, and appends an accuracy row
to `logs/dating/accuracy.jsonl`.

Ported from <v1-reference-install> PR #534. Chassis adaptations vs <v1-reference-install> source:
  - CHASSIS_HOME env var used instead of $CHASSIS_HOME hardcoded root
  - RHL picks dir: ${CHASSIS_HOME}/rhl-picks/{like,super-like,pass,no-opinion}/
    (installer-agnostic; <v1-reference-install> used ~/Desktop/dating-swipes/seans-picks/)
  - TASTE_CALIBRATE + CLIP_VENV resolved relative to CHASSIS_HOME

Filename contract (set by the swipe subagent at screenshot time):
    Name_Age_App_YYYY-MM-DD_subagentAction.png
    e.g. Ana_33_Hinge_2026-05-08_like.png
         Adriana_unk_Hinge_2026-05-02_pass.png

Installer's pick = the rhl-picks/<bucket>/ folder containing the file.
Subagent's prior decision = the trailing _<action>.{png,jpg,jpeg} segment.

Buckets:
    rhl-picks/like         - installer LIKED
    rhl-picks/super-like   - installer SUPER-LIKED
    rhl-picks/pass         - installer PASSED
    rhl-picks/no-opinion   - installer had no strong opinion (excluded from accuracy)

Outcomes per (installer_pick, subagent_action) pair:
    installer=like + subagent=like        -> TP (true positive)
    installer=like + subagent=pass        -> FN (false negative)  -> taste-refs/positive/
    installer=super-like + subagent=pass  -> FN (high priority recovery) -> taste-refs/positive/ + recovery queue
    installer=super-like + subagent=like  -> TP (agreement)
    installer=pass + subagent=pass        -> TN (true negative)
    installer=pass + subagent=like        -> FP (false positive)  -> left in rhl-picks/pass for manual curation
    installer=no-opinion + *              -> excluded
    incoming-like (installer got liked first) - counted as TP regardless

Recovery queue (closes the second-chance loop on Hinge):
    Each FN gets an entry in `logs/dating/recovery_queue.jsonl` with shape:
      {"name": "Adriana", "age": "unk", "platform": "Hinge",
       "date": "2026-05-02", "added_at": "2026-05-09T...", "status": "pending"}
    The dating-recovery-list.py companion script consumes this. The swipe
    subagent invokes the companion when Hinge offers the "show passed profiles"
    end-of-feed prompt.

Usage:
    python plugins/dating/scripts/dating-reconcile.py            # dry-run report
    python plugins/dating/scripts/dating-reconcile.py --apply    # commit changes
    python plugins/dating/scripts/dating-reconcile.py --apply --no-recalibrate
                                                                  # skip CLIP rebuild
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

CHASSIS_HOME = Path(os.environ.get("CHASSIS_HOME", Path(__file__).resolve().parent.parent.parent.parent))
PLUGIN_DIR = Path(__file__).resolve().parent.parent

RHL_PICKS_ROOT = CHASSIS_HOME / "rhl-picks"
RHL_PROCESSED = CHASSIS_HOME / "rhl-processed"
TASTE_POS = CHASSIS_HOME / "data" / "dating" / "taste-refs" / "positive"
ACCURACY_LOG = CHASSIS_HOME / "logs" / "dating" / "accuracy.jsonl"
RECOVERY_QUEUE = CHASSIS_HOME / "logs" / "dating" / "recovery_queue.jsonl"
TASTE_CALIBRATE = PLUGIN_DIR / "scripts" / "taste-calibrate.py"
CLIP_VENV = CHASSIS_HOME / ".venv-clip"

NAME_RE = re.compile(
    r"^(?P<name>[^_]+)_(?P<age>[^_]+)_(?P<platform>[^_]+)_(?P<date>\d{4}-\d{2}-\d{2})_(?P<action>[a-z\-]+)\.(?:png|jpe?g)$",
    re.IGNORECASE,
)
NAME_RE_4SEG = re.compile(
    r"^(?P<name>[^_]+)_(?P<platform>[^_]+)_(?P<date>\d{4}-\d{2}-\d{2})_(?P<action>[a-z\-]+)\.(?:png|jpe?g)$",
    re.IGNORECASE,
)

BUCKET_LIKE = "like"
BUCKET_SUPER = "super-like"
BUCKET_PASS = "pass"
BUCKET_NO_OPINION = "no-opinion"


class Pick(NamedTuple):
    installer_bucket: str  # like / super-like / pass / no-opinion
    subagent_action: str   # like / pass / super-like / incoming-like
    name: str
    age: str
    platform: str
    date: str              # YYYY-MM-DD
    file_path: Path


def parse_picks() -> list[Pick]:
    picks = []
    for bucket in (BUCKET_LIKE, BUCKET_SUPER, BUCKET_PASS, BUCKET_NO_OPINION):
        bucket_dir = RHL_PICKS_ROOT / bucket
        if not bucket_dir.is_dir():
            continue
        for fp in bucket_dir.iterdir():
            if not fp.is_file():
                continue
            m = NAME_RE.match(fp.name)
            age = ""
            if m:
                age = m["age"]
            else:
                m = NAME_RE_4SEG.match(fp.name)
                if m:
                    age = "unk"
            if not m:
                if fp.name == ".DS_Store":
                    continue
                print(f"WARN: filename does not match contract, skipping: {fp.name}", file=sys.stderr)
                continue
            picks.append(Pick(
                installer_bucket=bucket,
                subagent_action=m["action"].lower(),
                name=m["name"],
                age=age,
                platform=m["platform"],
                date=m["date"],
                file_path=fp,
            ))
    return picks


def classify(pick: Pick) -> str:
    if pick.installer_bucket == BUCKET_NO_OPINION:
        return "no_opinion"
    subagent_liked = pick.subagent_action in ("like", "super-like", "incoming-like")
    installer_positive = pick.installer_bucket in (BUCKET_LIKE, BUCKET_SUPER)
    if installer_positive and subagent_liked:
        return "tp"
    if installer_positive and not subagent_liked:
        return "fn"
    if not installer_positive and not subagent_liked:
        return "tn"
    return "fp"


def write_recovery_entry(pick: Pick, dry_run: bool) -> None:
    entry = {
        "name": pick.name,
        "age": pick.age,
        "platform": pick.platform,
        "date": pick.date,
        "installer_bucket": pick.installer_bucket,
        "added_at": datetime.now(timezone.utc).isoformat(),
        "status": "pending",
        "screenshot_basename": pick.file_path.stem,
    }
    if dry_run:
        return
    RECOVERY_QUEUE.parent.mkdir(parents=True, exist_ok=True)
    with RECOVERY_QUEUE.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def copy_to_positive(pick: Pick, dry_run: bool) -> bool:
    if dry_run:
        return True
    TASTE_POS.mkdir(parents=True, exist_ok=True)
    target = TASTE_POS / pick.file_path.name
    if target.exists():
        return False
    shutil.copy2(pick.file_path, target)
    return True


def archive(pick: Pick, dry_run: bool) -> None:
    if dry_run:
        return
    RHL_PROCESSED.mkdir(parents=True, exist_ok=True)
    target = RHL_PROCESSED / pick.file_path.name
    if target.exists():
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = RHL_PROCESSED / f"{pick.file_path.stem}.{ts}{pick.file_path.suffix}"
    shutil.move(str(pick.file_path), str(target))


def archive_fp_curated(pick: Pick) -> None:
    """FPs stay in rhl-picks/pass/ for manual curation review.
    Age-based passes would poison the negative embedding - leave them alone."""
    pass  # intentional no-op


def rebuild_clip_embeddings() -> int:
    python = CLIP_VENV / "bin" / "python"
    if not python.exists():
        print(
            f"WARN: {python} not found - skipping CLIP rebuild. "
            f"Run 'source {CLIP_VENV}/bin/activate && python {TASTE_CALIBRATE}' manually.",
            file=sys.stderr,
        )
        return -1
    try:
        r = subprocess.run(
            [str(python), str(TASTE_CALIBRATE)],
            cwd=str(CHASSIS_HOME),
            timeout=300,
        )
        return r.returncode
    except Exception as e:
        print(f"WARN: taste-calibrate failed: {e}", file=sys.stderr)
        return -1


def append_accuracy(counts: dict, fn_added: int, fp_review: list, recovery_added: int, dry_run: bool) -> None:
    if dry_run:
        return
    total_decided = counts["tp"] + counts["tn"] + counts["fp"] + counts["fn"]
    accuracy = (counts["tp"] + counts["tn"]) / total_decided if total_decided else None
    row = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "ts": datetime.now(timezone.utc).isoformat(),
        "tp": counts["tp"],
        "tn": counts["tn"],
        "fp": counts["fp"],
        "fn": counts["fn"],
        "no_opinion": counts["no_opinion"],
        "accuracy": round(accuracy, 4) if accuracy is not None else None,
        "fn_added_to_positive": fn_added,
        "fp_pending_review": fp_review,
        "recovery_queue_appended": recovery_added,
    }
    ACCURACY_LOG.parent.mkdir(parents=True, exist_ok=True)
    with ACCURACY_LOG.open("a") as f:
        f.write(json.dumps(row) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="commit changes (default: dry run)")
    ap.add_argument("--no-recalibrate", action="store_true", help="skip taste-calibrate.py rebuild")
    args = ap.parse_args()

    if not RHL_PICKS_ROOT.is_dir():
        print(
            f"ERROR: {RHL_PICKS_ROOT} not found.\n"
            "Create ${CHASSIS_HOME}/rhl-picks/{like,super-like,pass,no-opinion}/ and drag "
            "screenshots from swipe sessions into the appropriate bucket.",
            file=sys.stderr,
        )
        return 1

    picks = parse_picks()
    if not picks:
        print("No picks to process. rhl-picks/ folders empty.")
        return 0

    counts = {"tp": 0, "tn": 0, "fp": 0, "fn": 0, "no_opinion": 0}
    fn_added = 0
    fp_review = []
    recovery_added = 0

    for pick in picks:
        outcome = classify(pick)
        counts[outcome] += 1

        if outcome == "fn":
            added = copy_to_positive(pick, dry_run=not args.apply)
            if added:
                fn_added += 1
            write_recovery_entry(pick, dry_run=not args.apply)
            recovery_added += 1
            archive(pick, dry_run=not args.apply)
        elif outcome in ("tp", "tn", "no_opinion"):
            archive(pick, dry_run=not args.apply)
        elif outcome == "fp":
            archive_fp_curated(pick)
            fp_review.append(pick.file_path.name)

    total_decided = counts["tp"] + counts["tn"] + counts["fp"] + counts["fn"]
    accuracy = (counts["tp"] + counts["tn"]) / total_decided if total_decided else 0.0

    print(f"\n=== RHL Reconcile ({'APPLY' if args.apply else 'DRY RUN'}) ===")
    print(f"  TP: {counts['tp']}  TN: {counts['tn']}  FP: {counts['fp']}  FN: {counts['fn']}  no-opinion: {counts['no_opinion']}")
    print(f"  Accuracy: {accuracy:.1%}")
    print(f"  False-negatives -> taste-refs/positive/: {fn_added}")
    print(f"  Recovery queue appended: {recovery_added}")
    if fp_review:
        print("  False-positives left in rhl-picks/pass/ for manual curation:")
        for fn in fp_review:
            print(f"    - {fn}")

    if args.apply:
        if not args.no_recalibrate and fn_added > 0:
            print("\nRebuilding CLIP positive embeddings (taste-calibrate.py)...")
            rc = rebuild_clip_embeddings()
            if rc == 0:
                print("  taste_pos.npy rebuilt.")
            elif rc == -1:
                print("  Skipped (venv missing or run failed) - rebuild manually.")
            else:
                print(f"  taste-calibrate exited rc={rc}", file=sys.stderr)
        elif args.no_recalibrate:
            print("\nSkipped CLIP rebuild (--no-recalibrate).")
        elif fn_added == 0:
            print("\nSkipped CLIP rebuild (no new positives added).")

        append_accuracy(counts, fn_added, fp_review, recovery_added, dry_run=False)
        print(f"\nAccuracy row appended to {ACCURACY_LOG.relative_to(CHASSIS_HOME)}")
        if recovery_added > 0:
            print(f"Recovery queue at {RECOVERY_QUEUE.relative_to(CHASSIS_HOME)} ({recovery_added} new entries)")
            print("Run dating-recovery-list.py to consume the queue on the next Hinge passed-profiles pass.")
    else:
        print("\nDry run complete. Re-run with --apply to commit changes.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
