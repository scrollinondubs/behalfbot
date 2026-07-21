# Release notes prompt

Turn the JSON emitted by `chassis/scripts/release-material.sh <version>` into release notes for a chassis version, then publish them with `gh release create`.

## Input

```bash
./chassis/scripts/release-material.sh 0.2.0 > /tmp/material.json
```

The JSON carries: the version and its predecessor, the exact commit range (derived from `chassis/VERSION` history, so it is precisely what an install receives moving between those two versions), every commit with parsed conventional-commit type and scope plus the files it touched, every merged PR with title and body, the surfaces touched, and the CHANGELOG section the maintainer already wrote.

## Who reads these

Operators running a behalf.bot install. They did not write the code and do not follow the repo. They want to know one thing: **what is different on my machine after this update, and do I have to do anything.**

They are not release engineers. "Refactored the adapter factory" tells them nothing. "Notion now accepts documents longer than 100 blocks, so full-length briefings stop failing" tells them everything.

## Rules

**Group by what changed for the operator, not by commit type.** A `fix:` and a `refactor:` that together make one feature work are one bullet. Twelve commits that produced no observable change are zero bullets, or one line saying internal cleanup. Never emit a commit list. If the notes read like `git log`, start again.

**Lead with the reason to care.** Each bullet opens with the effect, not the mechanism. Mechanism goes second, and only when an operator could act on it.

**Say what an operator must do.** Config keys they need to set, credentials to add, manual steps, anything that changes on first boot after the update. If the answer is nothing, say nothing is required - that is useful information, not filler.

**Reconcile against `changelog_section` and report disagreements.** The CHANGELOG is a claim made when the code was written. Check it against the commits and the diff. If the CHANGELOG says a behaviour changed and no commit implements it, **say so in the notes** rather than repeating the claim. Release notes that repeat an unverified CHANGELOG launder an error into an announcement.

**Never assert a feature works because a commit exists.** Shipping-but-inert is a live defect class in this repo. State what was verified and how. If nothing was verified, say the change is untested rather than implying otherwise.

**Do not invent scope.** Every claim must trace to a commit, a PR body, or the diff. Nothing in the material means nothing in the notes.

**Breaking changes go at the top**, under a heading, with the manual review step spelled out. When `breaking` is true the auto-updater already forces `--force` mode, so the notes must tell the operator what to review before they run it.

**No em dashes.** Use " - " (space hyphen space).

## Shape

```markdown
## What you get

- Effect first, in one sentence. Mechanism second, if it matters.

## Breaking changes        <- only when breaking is true

- What breaks, what to review, what to run.

## What you need to do

- Config or credentials, or "nothing - this applies on the next boot".

## Fixed

- Bugs an operator could have hit, described as symptoms rather than causes.

## Internal

- One line. Not a list.
```

Drop empty sections rather than writing "none".

## Publishing

```bash
gh release create "v${VERSION}" \
  --repo scrollinondubs/behalfbot \
  --target "$(jq -r .release_sha /tmp/material.json)" \
  --title "chassis v${VERSION}" \
  --notes-file /tmp/notes.md
```

`--target` pins the tag to the commit that set `chassis/VERSION`, so the tag marks the release rather than wherever `main` has drifted to. That matters for backfilled releases, where main is far ahead.

**Backfilling:** `release-material.sh --unreleased` lists versions with no GitHub release. Work oldest first so the release list reads in order.

**Check before creating.** `gh release create` on an existing tag fails; on a new one it publishes immediately and the notification goes out. Confirm the version is genuinely unreleased, and have a human read the notes first.
