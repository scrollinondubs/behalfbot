<!--
Thanks for contributing. CONTRIBUTING.md has the full detail; this template is
just the shape a reviewer needs. Delete any section that does not apply rather
than writing "n/a" in it.
-->

## What this changes

<!-- One or two sentences. What is different after this merges? -->

## Why

<!-- The problem, not the patch. Link an issue if there is one - anything
non-trivial should have started as one. -->

## How it was verified

<!-- What you actually ran or observed, not what you expect to happen. "CI is
green" is fine for a docs change; anything behavioural needs more. -->

## Checklist

- [ ] Every commit is signed off (`git commit -s`) - CI enforces this, see CONTRIBUTING.md
- [ ] No credentials, tokens, or install-specific values (names, channel IDs, home paths, timezones)
- [ ] Safety rules stay at the hook layer, not moved into a prompt or a CLAUDE.md
- [ ] Any new heartbeat gathers cheaply first and invokes the model only when there is real work
- [ ] I read CONTRIBUTING.md's "what gets a PR declined" list

## Anything you are unsure about

<!-- Genuinely useful. Naming the part you are least confident in gets you a
better review than presenting it as finished. -->
