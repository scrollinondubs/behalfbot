---
name: software-engineering
description: Default for ANY code work in the principal's repos. Branch hygiene, worktrees, PR conventions, issue shape, testing, CI, deployment verification, secrets handling, sub-agent delegation, post-merge sync. Use before starting any code task.
---

# Software Engineering - practices the assistant follows

**Use this skill for:** any task where the assistant writes, reviews, ships or modifies code in the principal's repos.

This skill is **prescriptive, not advisory**. Deviations need a stated reason in the PR description.

Chassis core: `chassis/scripts/bootstrap-claude-project.py` copies this file to `.claude/skills/software-engineering/SKILL.md` in the install, where Claude Code loads it. Install-specific rules (repo names, the agent account, review habits) go in that installed copy; a re-run never overwrites it.

---

## Branch hygiene

### Never push to main

Always work on a feature branch. Even one-line fixes get a branch and a PR. Never force-push to main. If main looks broken, fix forward with a new commit; do not rewrite history.

### Branch naming

| Prefix | Use for |
|---|---|
| `feat/<slug>` | new feature or endpoint |
| `fix/<slug>` | bug fix or regression patch |
| `docs/<slug>` | docs-only change |
| `chore/<slug>` | dep bump, CI tweak, tooling |
| `refactor/<slug>` | non-functional restructure |
| `test/<slug>` | test-only addition |

Suffix the issue number when there is one: `feat/invoice-export-42`.

### Rebase before the first push, merge after

```bash
git fetch origin main
git rebase origin/main   # before the first push only
```

Once a branch is pushed, catch up with `git merge origin/main` and a normal push. The chassis guardrail hook (`chassis/.claude/hooks/guardrails.sh`) blocks every force-push, `--force-with-lease` included. When PRs are squash-merged, the merge commit never reaches main.

### Worktrees, not stash or branch switching

Never `git stash` or switch branches in a checkout that anything else uses (another session, a scheduled job, a running service). Use a worktree:

```bash
git worktree add ../<repo>-<slug> -b feat/<slug> origin/main
```

The stash stack is shared across every worktree, so a bare `git stash pop` can land someone else's work in your tree. Remove the worktree when the PR merges.

### Commit by path

`git add <path>` followed by a plain `git commit` commits the whole index, including whatever a concurrent session staged. Use `git commit -- <paths>` so the commit takes only what you named.

---

## Build plans

Any build that needs the principal's input before code starts gets an html-plan (the `html-plan` skill): one page, decisions placed on the claims they change, the assistant's default pre-checked, one pasted response back. Do not build until the response arrives. Skip the plan when there is nothing to decide: just build.

---

## Pull requests

### One PR per concern

Each PR addresses one problem, even if it is a one-line change. Bundling unrelated changes makes review and rollback harder.

### Title

Conventional Commits: `<type>(<scope>): <subject>`, with the issue number when there is one. Example: `fix(billing): stop duplicate invoice on retry (#42)`.

### Body

```markdown
## Summary

<1-3 sentences on what and why>

Closes #<issue> (if applicable)

## Test plan

- [ ] <how to verify on the PR branch>
- [ ] <how to verify after merge in production>
```

For substantive PRs add root cause, tradeoffs ruled out, screenshots, or before/after log output.

### The principal merges, the assistant never does

The assistant never merges its own PRs; the principal reviews and merges. Every report that mentions a PR carries its full `https://github.com/...` URL so the principal can open it in one tap.

### Post-merge sync

After the principal merges, sync local main:

```bash
git status --short   # must be clean; if not, stop - never stash
git switch main
git pull --ff-only
```

In a shared checkout, do not switch branches at all: it should already be on main, so `git pull --ff-only` is the whole sync. Never `git checkout main -- .`.

---

## Issues

### Issue body

Every non-trivial issue includes:

1. **Context** - why it exists, with a link to the source (the principal's message, error report, prior PR)
2. **Problem** - what is broken or missing
3. **Solution** - proposed approach, with tradeoffs ruled out
4. **Acceptance criteria** - checkboxes for what "done" means
5. **Out of scope** - what this issue will not do
6. **Dependencies** - use GitHub's native "blocked by" / "blocks" relations, not labels or body markers

### Closing

Close with a comment stating the outcome: PR link, decision, or why it was deferred. Someone reading the closed issue later should understand what happened.

---

## Code style

- **Comments:** default to none. Comment only when the WHY is non-obvious: a hidden constraint, a subtle invariant, a workaround for a specific bug. Never comment WHAT the code does, and never reference the current task or PR in a comment.
- **No em dashes** in code comments, commit messages, PR bodies, issue text, docs or UI copy. Use " - " (space-dash-space).
- **Type safety over runtime checks.** TypeScript: avoid `any`. Python: type hints on signatures. Validate at system boundaries (user input, external API responses), not inside trusted code paths.
- **No complexity for hypothetical futures.** A bug fix does not need surrounding cleanup. Three similar lines beat a premature abstraction.
- **Match the repo.** Read the repo's `CLAUDE.md` and surrounding code first. Follow existing naming and patterns. Never assume a library is available; check the manifest and imports.

---

## Dependencies

Treat any package name you have not used in this repo before as unverified until you check it against the registry: age, download count, a linked source repo. Attackers register names that models hallucinate. Flag any unverified install to the principal before adding it.

---

## Testing

- **Critical paths get E2E coverage.** Every revenue-bearing or trust-critical user path (signup, checkout, anything that moves money or data) gets an E2E test: a green-path test and at least one negative-path test.
- **Test isolation.** Tests run against an ephemeral database, never shared preview or production data. Tag test users in a greppable way (`e2e-test+<label>-<timestamp>@example.com`) for cleanup.
- **Do not mock what you can integration-test.** Mock only when the real thing is costly, slow or non-deterministic. Prefer recorded fixtures over hand-written ones.

---

## CI

Before merge, these must pass: lint and format, type check, unit tests, build, migration check (if applicable), E2E. If a check is broken or under repair, say so in the PR description. Never disable a check silently.

Never skip hooks (`--no-verify`) or signing (`--no-gpg-sign`) to ship. If a hook fails, fix the root cause.

---

## Verification

- **Exercise the changed code path end to end** before declaring done. Drive the actual flow, not just typecheck or unit tests. For web UI, look at it in a real browser.
- **State exactly what you ran and what you observed.** Never claim a passing result you did not see.
- **Check ground truth, not docs.** Before saying something is or is not deployed, running or configured, query the source (the host, the database, the endpoint, the process list).
- **After a production deploy:** hit a critical-path URL, confirm 200 and that key content renders. If it looks wrong, tell the principal right away with the rollback option; roll back only on the principal's go.

### Rollback discipline

Before a PR that touches anything stateful (migrations, env vars, external service config) goes to the principal, confirm the rollback path exists and say what it is in the PR body. Prefer additive migrations (add a column) over destructive ones (drop a column), so rollback is "deploy old code", not "restore from backup".

---

## Secrets

- Secrets never appear in code, commit messages, issue bodies, PR descriptions, docs, logs or plans. Scan the diff before every commit.
- Key names go in `.env.example`; values go in `.env` (gitignored), the host's encrypted env vars, or the password manager.
- Rotate a secret when it crosses a new trust boundary (logged externally, leaked to a third party, used by a decommissioned service). Do not rotate reflexively.

---

## Git identity

The assistant operates as its own GitHub account (the agent-side identity from `docs/installer-homework.md`), never as the principal. Check before any git or PR operation:

```bash
gh auth status   # expect: Logged in to github.com account <agent account>
```

If it shows the wrong account, fix that before doing anything else.

---

## Sub-agent delegation

- **Delegate non-trivial code work** to the `software-engineering` sub-agent. Give it the goal, the repo, what you already ruled out, and any plan or contract file. It is stateless, so the brief is all it knows.
- **One agent per repo.** Run them in parallel only when a contract file pins the interface between them.
- **Use read-only search agents for research** (mapping code, `path:line` lookups) so file dumps stay out of the main context.
- **Audit every return.** Agents overreach. Check the PR file list and recent pushes against the brief before reporting anything to the principal.

---

## When this skill applies

**Applies:** editing code in the principal's repos, reviewing a PR, opening an issue, shipping a fix, designing a feature.

**Does not apply:** writing copy or outreach, non-engineering decisions (those go to the principal), throwaway scripts that will not ship.
