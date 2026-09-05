# Colima: one owner, and how it recovers from an unclean shutdown

> macOS installs only. On Linux, Docker runs natively and nothing in this
> document applies.
>
> Background: scrollinondubs/new-jaxity#550 (postmortem, 2026-09-05).

## The failure, exactly

Someone hard-powers-off the Mac. Lima leaves its runtime files behind in
`~/.colima/_lima/<instance>/`:

```
ha.pid   vz.pid   ha.sock   ssh.sock
```

After the reboot, the PIDs in those files have been reissued by the kernel to
unrelated processes. On the reference Mac Mini the stale PID was 789, which by
then was `sirittsd`. Colima reads the file, concludes the VM is alive, and every
command after that talks to a socket nobody is listening on.

What you see:

```
$ colima list
PROFILE    STATUS     ARCH       CPUS    MEMORY    DISK     RUNTIME
default    Broken     aarch64    4       6GiB      30GiB    docker

$ colima status
colima is not running

$ docker ps
Cannot connect to the Docker daemon at unix:///Users/<user>/.colima/default/docker.sock.
Is the docker daemon running?

$ colima start
FATA[0000] errors inspecting instance: [failed to get Info from
".../ha.sock": dial unix .../ha.sock: connect: connection refused]

$ tail ~/.colima/default/daemon/daemon.log
waiting 5 secs for VM
waiting 5 secs for VM
waiting 5 secs for VM        # forever
```

Every container on the box stays down with it. On the reference install that was
Postgres, the note-taking app, and the chassis container running the heartbeat
dispatcher, which is to say the whole install.

**`colima start` can never clear this.** It reads the same stale files on every
attempt. A launchd job that retries it retries the same failure.

## The fix, and the one command that does it

```sh
colima stop --force && colima start
```

`stop --force` removes the stale pid and socket files and flips the profile from
`Broken` back to `Stopped`. It is **non-destructive**: the VM's `disk` image, and
therefore every Docker volume living on it, is untouched.

### Never reach for these

```sh
colima delete            # destroys the VM disk image and every volume on it
colima start --reset     # same
```

Both are commonly suggested for a wedged Colima and both are wrong here. The
disk image is where the container data lives. On the reference install that is a
20GB image holding Postgres and the note-taking app's workspace with no other
copy on the box. `chassis/scripts/colima-ensure.sh` is structurally incapable of
running either, and its test suite asserts that on every code path.

## The chassis shape: one owner, everyone else waits

**`com.behalfbot.colima` is the only job on the machine allowed to start
Colima.** It is a LaunchDaemon that runs
`chassis/scripts/colima-ensure.sh`.

The wrapper:

1. Exits 0 immediately if `docker ps` already answers.
2. Reads the profile status. If it is `Broken`, it skips straight to the forced
   stop rather than burning a boot minute on a `colima start` that is guaranteed
   to fail.
3. Otherwise tries a plain `colima start`, then waits for `docker ps` to answer.
4. If either the start or the docker probe fails, runs `colima stop --force` and
   retries `colima start` exactly once.
5. Verifies `docker ps` answers before reporting success. A `colima start` that
   exits 0 while the socket refuses connections is exactly the state this exists
   to catch, so the exit code is never trusted on its own.
6. Exits 1 and names what it tried if that did not work. There is no third
   escalation, on purpose.

It holds a lock (`${TMPDIR}/colima-ensure.<profile>.lock`) so concurrent
invocations cannot fight. Recovering the reference install by hand meant booting
out `postgres-watchdog`, `container-liveness` and `terminal-watchdog` first so
they would not race the repair; the lock is that, automated.

The lock treats a holder as abandoned on **age** as well as on pid liveness.
`kill -0` against a recycled pid succeeds for a completely unrelated process,
which is the same bug the whole document is about; a pid check alone would
deadlock the box permanently on it.

### Everything else waits for the socket

If a job needs Docker, it waits. It does not start Colima. The pattern chassis
already uses, at the top of
`chassis/scripts/templates/restart-discord.sh.template`:

```sh
for i in {1..30}; do
    if docker info >/dev/null 2>&1; then break; fi
    sleep 3
done
```

## Migrating an existing install

Two changes. The first is automatic, the second is not.

### 1. Replace the old colima plist

Installs from before this change have a hand-written
`/Library/LaunchDaemons/com.behalfbot.colima.plist` that runs `colima start -f`
directly, with `KeepAlive { SuccessfulExit: true }`. That KeepAlive rule respawns
the job when it exits **zero** and leaves it alone when it fails, so the one case
that needed a retry was the one case launchd never retried. On 2026-09-05 it sat
parked at exit status 1 while Docker stayed down.

Re-running the bootstrap replaces it in place. The label is unchanged, so
activation boots out the old job and installs the new one:

```sh
CUSTOMER_HOME=~/.behalfbot CHASSIS_HOME=~/behalfbot BOT_NAME=<bot> \
  bash chassis/scripts/bootstrap-customer-scripts.sh --activate-plists
```

Run it outside tmux (the discord-restart agent rebuilds the tmux server on
activation). Installing a daemon needs sudo; the script prompts rather than
sudoing behind you.

Verify:

```sh
sudo launchctl print system/com.behalfbot.colima | head
tail ~/.behalfbot/logs/scheduled/colima-ensure.log
```

### 2. Demote every other colima owner by hand

This is the part chassis cannot do for you: the duplicate owner usually lives in
a customer-written plist, and silently rewriting someone's own launchd job is not
something the bootstrap should do.

`bootstrap-customer-scripts.sh` scans `/Library/LaunchDaemons` and
`~/Library/LaunchAgents` on every plist render and warns about any job that
appears to start or restart Colima, following one level of indirection into the
shell scripts those plists invoke. It is a heuristic. Confirm before editing.

The reference install had two:

**`com.jax.postgres`** ran, inline in the plist:

```sh
colima status || colima start --cpu 4 --memory 6 --disk 30
cd ~/.behalfbot/db/postgres && docker compose up -d
```

Change the prefix to a wait:

```sh
for _ in $(seq 1 60); do docker info >/dev/null 2>&1 && break; sleep 3; done
cd ~/.behalfbot/db/postgres && docker compose up -d
```

**`com.jax.postgres-watchdog`** escalates to `colima restart` from
`watchdog-postgres.sh`. That one is already gated behind an explicit
`POSTGRES_WATCHDOG_ALLOW_COLIMA_RESTART=true` opt-in that defaults to off, so it
is not an owner in practice. Leave the default off. If you turn it on, you have a
second owner again, and you should know that you did.

## Sizing a new VM

`colima-ensure.sh` never passes resource flags to an existing profile. Sizing
policy belongs to whoever created the VM, not to a recovery script, and a
recovery run is the worst possible moment to reconfigure a VM.

For a first-ever creation, set `COLIMA_ENSURE_START_ARGS` in the environment:

```
COLIMA_ENSURE_START_ARGS="--cpus 4 --memory 6 --disk 30"
```

It is applied only when the profile does not exist yet. Otherwise just run
`colima start` by hand once with the sizing you want and let the wrapper take it
from there.

## When the wrapper gives up

It exits 1 and logs what it tried. Look at:

```sh
tail -50 ~/.behalfbot/logs/scheduled/colima-ensure.log
tail -50 ~/.colima/<profile>/daemon/daemon.log
tail -50 ~/.colima/_lima/colima*/ha.stderr.log
```

Do not escalate to `colima delete` or `colima start --reset`. If the VM is
genuinely unrecoverable, back the disk image up first:

```sh
cp ~/.colima/_lima/colima*/diffdisk /some/other/volume/
```

## Related

- `chassis/scripts/colima-ensure.sh` - the wrapper.
- `chassis/scripts/test-colima-ensure.sh` - its behavioural suite.
- `chassis/launchd/com.behalfbot.colima.plist.template` - the single owner.
- `docs/launchd-domains.md` - why this one is a daemon and the discord jobs are
  not.
- scrollinondubs/new-jaxity#550 - the postmortem.
