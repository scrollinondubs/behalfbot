# Bringing an installer's agent into a shared project

> Audience: an operator agent (the install that plans a project) handing that
> project to an installer's own chassis agent, then working alongside it.
> Companion to `docs/installer-homework.md` (getting the installer's box up) and
> `docs/discord-intake.md` (how the Discord channel plugin hears messages).

The operator plans the build. The installer's own agent builds it. The operator
stays on as CTO, reviewing PRs and ratifying design changes.

This checklist comes from the first run on the reference install, on 2026-11-24.
Every step below either worked that day or fixes something that broke.

## Roles

- **`<installer>`**: the stakeholder. Owns their install and approves changes to
  it. Signs off on product and design for the project.
- **`<agent>`**: the installer's chassis agent. Builds the project. Takes
  directives only from `<installer>`, who is its principal in chassis terms.
- **`<operator>`**: the operator agent. Plans the project, then acts as CTO from
  the back seat: reviews PRs, ratifies design deviations, grades `<agent>` and
  upgrades its skills.
- **`<principal>`**: the operator's principal. Owns the Discord server the shared
  channel lives in, plus the repos and accounts the operator uses. Approves
  anything that touches those.

Placeholders used below: `<agent host>` is the installer's box, `<agent user>` the
Linux user the install runs as, `<agent container>` its chassis container,
`<guild id>` the principal's Discord server, `<channel id>` the shared channel,
`<agent bot id>` and `<operator bot id>` the two bots' Discord user IDs.

## 0. Before you start

- [ ] `<principal>` has approved the project and named the installer channel it lives in.
- [ ] `<installer>` knows it is coming. `<agent>` will not start work on anyone
  else's say-so (correct behaviour, see step 6), so time the kickoff to the
  installer's waking hours in their timezone.
- [ ] You have SSH to `<agent host>`: `ssh -i ~/.ssh/<operator key> root@<agent host>`.

## 1. Plan the project in a repo the agent can live in

**Where the repo goes.**
- Create it **private, under the operator's own GitHub account**.
- A bot account cannot create repos under a personal user account it does not
  own. Only an org can grant that. So don't plan on creating it under
  `<principal>`'s user account.
- Transfer ownership to `<agent>`'s or `<installer>`'s account later, once
  `<agent>` has earned trust.

**What goes in it.** `<agent>`'s weak point is memory across sessions, so the repo
has to carry everything:
- `docs/<AGENT>-START-HERE.md`: how to clone, where the data is, env setup, build
  order, the per-issue loop, how to ask `<installer>` questions, and what must
  never be committed.
- `CLAUDE.md`: a start-of-session and end-of-session routine. Every session ends
  with a Handoff comment on the issue and an update to `docs/STATUS.md`.
- A PRD, the data model, the screens and the design tokens. Every open decision
  carries a default.
- Small, standalone issues grouped into milestones, each with acceptance criteria
  and test steps.

**Design first**, in this order:
1. A clickable prototype of every screen, with dummy data, on a shareable link.
2. `<installer>` signs off the flows in an issue.
3. Only then: the data model, the API and the wiring.

**Gates written into `CLAUDE.md`:**
- The first 3 PRs stop and wait for review.
- After that, no more than 2 open PRs at a time.
- `<agent>` never merges its own PRs.
- Any deviation from the repo docs is proposed in an issue labelled `needs-cto`.
  `<operator>` ratifies it before it gets built.

**No personal data or private content in git.**
- `<installer>`'s exports live in a gitignored `intake/` folder.
- CI fails if anything under `intake/`, or any `.env`, is committed.

**Invite `<agent>`'s GitHub user** as a collaborator with `push`:

```
gh api -X PUT repos/<owner>/<repo>/collaborators/<agent github user> -f permission=push
```

## 2. Put the handoff data where the agent can actually see it

**Trap, hit on the first run.** `<agent>` runs inside `<agent container>`, which
only mounts its install directory (`$CUSTOMER_HOME`, usually
`/home/<agent user>/.behalfbot`). Data copied anywhere else under
`/home/<agent user>/` was invisible to it.

- **Copy into the mounted install directory, under a gitignored path:**
  `rsync ... root@<agent host>:/home/<agent user>/.behalfbot/data/<project>-intake/`,
  then `chown -R <agent user>:<agent user>` and `chmod 700`.
- **Path inside the container:** that folder appears as
  `/app/customer/data/<project>-intake`.
- **Verify from inside the container:**
  `docker exec <agent container> ls /app/customer/data/<project>-intake`.
- **If unsure what is mounted, check first:**
  `docker inspect <agent container> --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{println}}{{end}}'`.

## 3. Get the agent's bot into the shared Discord channel

The installer channel lives in `<principal>`'s server (`<guild id>`). `<agent>`'s
bot is usually only in its own server.

1. **`<principal>` adds the bot.** Only a server admin can do this. Send them the
   OAuth link with **no server-wide permissions**:
   `https://discord.com/oauth2/authorize?client_id=<agent bot id>&scope=bot&permissions=0`
2. **Restrict the bot to the one channel.** If `@everyone` can view most channels
   in the server, a bare join lets `<agent>` read the whole community. With
   `<principal>`'s explicit OK, set member overwrites for `<agent bot id>`:
   - On `<channel id>`, allow `117824` (view, send, read history, embed, attach, react).
   - On every other channel and category in `<guild id>`, deny `1024` (view).
   - Category overwrites do not cascade at runtime, so set each channel individually.
   - The chassis `guardrails.sh` hook blocks a mutating `curl` (`-X PUT` and the
     like) to any host not in `CHASSIS_HTTP_ALLOWLIST`, discord.com included. Use
     a short Python script that sends a User-Agent instead. On the first run this
     came to 1 allow, 37 denies, 0 failures.
3. **Verify with `<agent>`'s own bot token, from `<agent host>`.** `GET
   /channels/<channel id>/messages` must return 200, and the same call on any
   other channel must return 403.
4. **Channels created later are visible to `<agent>` by default.** Add the same
   deny whenever one is created.

## 4. Open both gates on the agent's side

`<agent>` needs two separate permissions: one to hear the channel, one to post in
it. On the first run the agent had the first but not the second, which looked
like "typing... then nothing".

| Gate | Where | What to set |
|---|---|---|
| Hearing | `/home/<agent user>/.claude/channels/discord/access.json`, `groups.<channel id>` | `requireMention: true`, and `allowFrom`: `<installer>`, `<principal>` and `<operator bot id>` |
| Speaking | the install's outbound gate hook, if any (a PreToolUse hook under `/home/<agent user>/.behalfbot/.claude/hooks/` that allowlists the channels the agent may post to) | append `<channel id>` to its channel list |

- **Before editing:** back up both files (`.bak-<date>`). After editing the hook,
  check it with `bash -n`.
- **The install's channel-add script, if any**, may set both gates in one go. Check
  what it writes: on the first run it set `requireMention: false` and
  `allowFrom: [<installer>]` only, which suits the installer's own channels but
  not a shared one. Adjust `access.json` afterwards.
- **Multi-party reminder.** If `<agent>`'s install uses the chassis
  `principal-policy-hook.sh`, add `<channel id>` to its
  `CHASSIS_MULTI_PARTY_CHANNELS`. The hook then reminds `<agent>`, on every
  message from someone else in that channel, that only `<installer>` gives it
  directives.
- **Confirm the container sees the edits:**
  `docker exec <agent container> grep -c <channel id> /app/customer/.claude/hooks/<outbound gate hook>`
- **When it "doesn't answer", read the agent's own pane:**
  `su - <agent user> -c "tmux capture-pane -p -t <session> -S -60"`
  On the first run the pane showed the exact hook error that blocked the reply.
- **Tell `<installer>` what you changed** on their install, in the channel. It is their box.

## 5. Smoke test before the kickoff

1. Post `@<agent> connectivity test from <operator>: reply "ack" here`. The agent
   **does** receive messages from bots: on the first run it acked the operator's test.
2. If there is no ack within 2 minutes, go back to step 4 and read its pane.

## 6. Kickoff

Post in the installer channel and @mention `<agent>`. Cover only what the repo
does not:

- the data path inside its container
- where to ask `<installer>` questions, and that every question carries a default
- that `<operator>` reviews its first 3 PRs
- that every session ends with a Handoff comment
- that every "can't" names what was tried and the next best route
- a request to reply with a short plan

**Expect `<agent>` to wait for its own installer's go** before touching the repo or
the data. On the first run it did exactly that. It is the trust behaviour
installers ask for, so don't route around it. Tell `<installer>` what is waiting
on their yes.

## 7. Running it as CTO

- **Review `<agent>`'s PRs on GitHub, not in Discord.** GitHub is the durable
  record both sides can see.
- **Ratify or reject each `needs-cto` issue promptly.** A stalled ratification
  stalls the agent.
- **Grade `<agent>`** against the repo's acceptance criteria and the gates above.
  Where it falls short in engineering or design, add or upgrade a skill on its
  install (with `<installer>`'s OK), not just a review comment.
- **Write what you learn back into this doc.**

## Failure log: observed on the first run (2026-11-24)

| Symptom | Cause | Fix |
|---|---|---|
| Agent not in the channel | Its bot was only in the installer's own server | `<principal>` authorised the OAuth link with `permissions=0` |
| `<principal>`'s @mention ignored | `allowFrom` lacked `<principal>` | Added them to `access.json` |
| "Typing", then silence | The outbound gate hook blocked the reply | Added the channel to the hook's channel list |
| Agent "can't see" the intake folder | Container mounts only `$CUSTOMER_HOME` | Moved the data to `$CUSTOMER_HOME/data/` |
| Agent could see the server's community channels | `@everyone` has view permission | Per-channel member deny overwrites |
