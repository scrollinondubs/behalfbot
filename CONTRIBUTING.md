# Contributing to the Behalf.bot chassis

Thanks for looking. This document exists so you know the bar before you spend a
weekend on something, and so a "no" from us is a link rather than an argument.

## Read this first: what this project is, and what it is not

The chassis is **opinionated infrastructure for one person's always-on agent**.
It is not a framework trying to serve every use case. A change that makes it
more general at the cost of making it harder to reason about will usually be
declined, and that is not a judgement on the code.

The line that matters most: **safety lives at the hook layer, not in prompts.**
`chassis/.claude/hooks/guardrails.sh` is deterministic and survives a context
reset; a rule written into a CLAUDE.md does not. Changes that move enforcement
from the first place to the second will be declined regardless of how well they
are written. See [`docs/security.md`](docs/security.md).

## Licence and the sign-off

This repo is under the **O'Saasy Licence** (see [`LICENSE`](LICENSE)), which is
source-available rather than OSI-approved open source. That matters for
contributions: with MIT or Apache there is a well-understood convention that a
contribution arrives under the project's licence. There is no such convention
for a custom licence, so we ask for it explicitly.

We use the [Developer Certificate of Origin](https://developercertificate.org/).
**This is not a CLA.** There is no form, no signing ceremony, and no copyright
assignment - you keep your copyright. It is one line in your commit message:

```
Signed-off-by: Your Name <your@email.com>
```

`git commit -s` adds it for you. It records that you wrote the change, or have
the right to submit it, and are submitting it under this project's licence.

CI checks it. If you forget:

```bash
git commit --amend -s --no-edit        # most recent commit
git rebase --signoff origin/main       # every commit on your branch
git push --force-with-lease
```

## Before you open a PR

**Open an issue first for anything non-trivial.** A typo fix or a clearly-scoped
bug fix can go straight to a PR. Anything that adds a dependency, changes an
interface, adds a heartbeat, or touches the hook layer should start as an issue,
because the most common reason a well-written PR gets declined is that it
rebuilds something deliberately rejected earlier.

**Check the closed issues and merged PRs.** Same reason.

## What CI will check

Every PR runs, on every change regardless of paths:

| Check | What it wants |
|---|---|
| `check-no-customer-state` | No customer-specific state committed into the chassis tree |
| `diff-against-canonical` | The plugin fetcher seed still matches the canonical list |
| `dco` | Every non-merge commit has a `Signed-off-by` line |

Shell changes additionally run `shellcheck` at `severity: error`. The bar is
"contains no real bug", not "matches our style" - do not spend time on lint
preferences.

Some workflows are path-filtered and will not run on your PR. That is expected
and is not something being skipped: only checks that can run on every PR are
required to pass.

## What gets a PR declined

Stated plainly so you can avoid all of it:

- Moving a safety rule from the hook layer into a prompt or a CLAUDE.md
- Committing credentials, tokens, or anything customer-specific
- Adding a dependency to do something the standard library or an existing
  dependency already does
- Broad reformatting or style-only churn mixed into a functional change
- A new heartbeat with no gather-first condition. The dispatcher's whole design
  is "check cheaply, invoke the model rarely" - roughly 96 ticks a day for about
  4 model calls. A heartbeat that fires the model on a schedule regardless of
  whether there is work breaks the economics for every install.
- Anything that makes the chassis assume a specific person's setup. If it
  hardcodes a name, a channel ID, a path under someone's home directory, or a
  timezone, it belongs in an install overlay rather than here.

## Security

**Do not open a public issue for a vulnerability.** See
[`SECURITY.md`](SECURITY.md) for the private disclosure channel.

## Review, and what to expect

This is a small project with one maintainer. Reviews are best-effort, not
same-day. A PR that sits for a few days has not been ignored.

An assistant does a first pass on inbound PRs - size, paths touched, CI state,
whether it duplicates earlier work - and surfaces a summary. **Every merge
decision is a human one.** Nothing is merged by a bot.
