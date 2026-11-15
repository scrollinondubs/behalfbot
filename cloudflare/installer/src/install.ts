// One install's state machine. InstallDO (index.ts) is a thin shell around
// this so the node tests can drive it with node:sqlite, a Map and a fake
// fetch. Everything that writes to the journal or to Discord goes through
// appendJournal / say, which redact first.

import { DiscordClient, channelName } from './discord.ts'
import { Redactor } from './redact.ts'
import {
  allGreen,
  capBreach,
  gcBase,
  gcDecision,
  startDecision,
  HOUR,
  WALL_CLOCK_CAP,
  type HomeworkState,
} from './rules.ts'
import {
  HOMEWORK_ITEMS,
  HOMEWORK_LABELS,
  INSTALL_DDL,
  type InstallListing,
  type InstallRow,
  type InstallStatus,
  type JournalKind,
} from './schema.ts'
import { generateInstallKey, keyComment } from './sshkey.ts'
import { assertPinned, SshTransport, type RunOpts, type RunResult } from './transport.ts'
import type { CreatePayload } from './payload.ts'
import type { FetchLike, InstallerSecrets, KvLike, Reply, SqlLike } from './types.ts'

// Hetzner images log in as root. TODO(runbook): switch to a sudo user once
// the runbook creates one, and key removal follows whichever user we use.
const SSH_USER = 'root'

const K = {
  privateKey: 'ssh_private_key',
  publicKey: 'ssh_public_key',
  // Hard requirement 3: held here only until the vault on the box is up,
  // then written there and deleted. TODO(key-form): the key form sets it.
  hetznerToken: 'hetzner_token',
  origin: 'origin',
  discordUsername: 'discord_username',
  variant: 'variant',
  runStartedAt: 'run_started_at',
  gcEligibleSince: 'gc_eligible_since',
  gcRetryAfter: 'gc_retry_after',
  startNextCheck: 'start_next_check',
  startMissPosted: 'start_miss_posted',
  startChecks: 'start_checks',
} as const

const ITEM_RE = /^[a-z][a-z0-9_]{1,39}$/

export interface InstallDeps {
  sql: SqlLike
  kv: KvLike
  env: InstallerSecrets
  fetch: FetchLike
  now: () => number
  setAlarm: (atMs: number | null) => Promise<void>
  // Forwards a request to this install's SSH container instance.
  sendToBox: (installId: string, req: Request) => Promise<Response>
  // Pushes this install's listing row to the registry (GET /installs).
  sync: (listing: InstallListing) => Promise<void>
}

interface JournalOpts {
  step?: number
  stepName?: string
  attempt?: number
  exitCode?: number | null
  costCents?: number | null
}

interface EscalateOpts {
  step?: number
  stepName?: string
  attempts?: number
  customerNeeded?: boolean
}

export class InstallCore {
  private readonly d: InstallDeps
  private discordClient: DiscordClient | null = null
  private inflightInit: Promise<Reply> | null = null
  // Values that went to the box over stdin while this DO instance has been
  // alive. Redacted from anything written afterwards, not just that run.
  private readonly sessionSecrets: string[] = []
  private syncChain: Promise<void> = Promise.resolve()

  constructor(d: InstallDeps) {
    this.d = d
    for (const ddl of INSTALL_DDL) d.sql.exec(ddl)
  }

  private nowS(): number {
    return Math.floor(this.d.now() / 1000)
  }

  private discord(): DiscordClient {
    if (!this.discordClient) {
      this.discordClient = new DiscordClient({
        token: this.d.env.DISCORD_INSTALLER_BOT_TOKEN,
        guildId: this.d.env.DISCORD_GUILD_ID,
        fetch: this.d.fetch,
      })
    }
    return this.discordClient
  }

  row(): InstallRow | null {
    return (this.d.sql.exec('SELECT * FROM install LIMIT 1').toArray()[0] as InstallRow | undefined) ?? null
  }

  private requireRow(): InstallRow {
    const r = this.row()
    if (!r) throw new Error('install not initialised')
    return r
  }

  private update(fields: Partial<InstallRow>): void {
    const keys = Object.keys(fields)
    const sets = [...keys.map((k) => `${k} = ?`), 'updated_at = ?'].join(', ')
    const vals = keys.map((k) => (fields as Record<string, unknown>)[k] ?? null)
    this.d.sql.exec(`UPDATE install SET ${sets}`, ...vals, this.nowS())
    this.queueSync()
  }

  private queueSync(): void {
    const r = this.row()
    if (!r) return
    const listing: InstallListing = {
      install_id: r.install_id,
      source: r.source,
      purchase_id: r.purchase_id,
      customer_name: r.customer_name,
      discord_user_id: r.discord_user_id,
      channel_id: r.channel_id,
      status: r.status,
      scheduled_at: r.scheduled_at,
    }
    // A failed push leaves the listing stale until the next change; the
    // install row itself is unaffected.
    this.syncChain = this.syncChain.then(() => this.d.sync(listing)).catch(() => {})
  }

  // Every public entry point awaits this before returning so the registry
  // copy is current when the request or alarm ends.
  async flush(): Promise<void> {
    await this.syncChain
  }

  private homework(): { item: string; state: HomeworkState }[] {
    return this.d.sql.exec('SELECT item, state FROM homework ORDER BY rowid').toArray() as { item: string; state: HomeworkState }[]
  }

  // ---- redaction choke points ------------------------------------------

  private async redactor(): Promise<Redactor> {
    const r = new Redactor()
    r.add(await this.d.kv.get<string>(K.privateKey))
    r.add(await this.d.kv.get<string>(K.hetznerToken))
    for (const s of this.sessionSecrets) r.add(s)
    return r
  }

  async appendJournal(kind: JournalKind, body: string, o: JournalOpts = {}): Promise<void> {
    const red = await this.redactor()
    const installId = this.row()?.install_id ?? 'unknown'
    this.d.sql.exec(
      'INSERT INTO journal (install_id, step, step_name, attempt, kind, body, exit_code, cost_usd_cents, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
      installId,
      o.step ?? 0,
      red.redact(o.stepName ?? 'lifecycle'),
      o.attempt ?? 0,
      kind,
      red.redact(body),
      o.exitCode ?? null,
      o.costCents ?? null,
      this.nowS(),
    )
  }

  private async say(channelId: string | null, text: string, mention: string[] = []): Promise<boolean> {
    if (!channelId) return false
    const red = await this.redactor()
    try {
      await this.discord().postMessage(channelId, red.redact(text), mention)
      return true
    } catch (e) {
      await this.appendJournal('note', `discord post to ${channelId} failed: ${errText(e)}`)
      return false
    }
  }

  // ---- POST /installs ----------------------------------------------------

  async init(p: CreatePayload, installId: string, origin: string): Promise<Reply> {
    // Two POSTs for the same purchase can reach this DO at once; the second
    // waits for the first rather than creating a second channel.
    if (this.inflightInit) return this.inflightInit
    this.inflightInit = this.doInit(p, installId, origin).finally(async () => {
      this.inflightInit = null
      await this.flush()
    })
    return this.inflightInit
  }

  private async doInit(p: CreatePayload, installId: string, origin: string): Promise<Reply> {
    let row = this.row()
    if (!row) {
      const now = this.nowS()
      this.d.sql.exec(
        `INSERT INTO install (install_id, source, purchase_id, customer_name, customer_email, discord_user_id, channel_id,
          status, scheduled_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)`,
        installId,
        p.source,
        p.purchase_id,
        p.customer_name,
        p.customer_email,
        p.discord_user_id,
        p.scheduled_at === null ? 'homework' : 'scheduled',
        p.scheduled_at,
        now,
        now,
      )
      for (const item of HOMEWORK_ITEMS) {
        this.d.sql.exec("INSERT INTO homework (install_id, item, state, updated_at) VALUES (?, ?, 'pending', ?)", installId, item, now)
      }
      await this.d.kv.put(K.origin, origin)
      if (p.discord_username) await this.d.kv.put(K.discordUsername, p.discord_username)
      if (p.variant) await this.d.kv.put(K.variant, p.variant)
      const key = await generateInstallKey(installId)
      await this.d.kv.put(K.privateKey, key.privateKeyPem)
      await this.d.kv.put(K.publicKey, key.publicKey)
      await this.appendJournal('note', `install created from ${p.source}${p.purchase_id ? ` purchase ${p.purchase_id}` : ''}; per-install ed25519 key generated`)
      row = this.requireRow()
    }
    if (row.install_id !== installId) return { status: 409, body: { error: 'install_id mismatch' } }
    if (row.channel_id) return { status: 200, body: { install_id: row.install_id, channel_id: row.channel_id } }

    // Channel creation is retried on every repeat POST until it succeeds.
    try {
      let customerId = row.discord_user_id
      const username = await this.d.kv.get<string>(K.discordUsername)
      if (!customerId && username) {
        try {
          customerId = await this.discord().findMemberByUsername(username)
          if (customerId) this.update({ discord_user_id: customerId })
        } catch (e) {
          await this.appendJournal('note', `could not look up Discord user ${username}, creating the channel bot-only: ${errText(e)}`)
        }
      }
      const name = channelName(row.customer_name ?? username ?? 'customer')
      const channelId = await this.discord().createPrivateChannel(name, customerId)
      this.update({ channel_id: channelId })
      await this.appendJournal('note', `created private channel ${channelId}${customerId ? '' : ' (customer not resolved yet: bot-only until their Discord id is known)'}`)
      // TODO(stripe): when discord_username does not resolve, nobody but the
      // bot can see the channel. Resolve later and call allowMember.
      await this.postWelcome()
    } catch (e) {
      await this.appendJournal('note', `channel create failed, will retry on the next POST: ${errText(e)}`)
      return { status: 502, body: { error: 'discord_unavailable', install_id: installId } }
    }
    await this.arm()
    const r = this.requireRow()
    return { status: 201, body: { install_id: r.install_id, channel_id: r.channel_id } }
  }

  private homeworkLines(): string {
    return this.homework()
      .map((h) => `${h.state === 'green' ? '✓' : '○'} ${HOMEWORK_LABELS[h.item] ?? h.item}`)
      .join('\n')
  }

  private async postWelcome(): Promise<void> {
    const r = this.requireRow()
    const first = (r.customer_name ?? '').trim().split(/\s+/)[0]
    const hello = r.discord_user_id ? `Hi <@${r.discord_user_id}>.` : first ? `Hi ${first}.` : 'Hi.'
    const slot = r.scheduled_at
      ? `Your install is booked for <t:${r.scheduled_at}:F>.`
      : "Your install slot isn't confirmed yet. I'll post here when it is."
    // TODO(key-form): link the per-install homework and key page here.
    await this.say(
      r.channel_id,
      `${hello} This is your private install channel. ${slot}\n\nBefore then, please finish these:\n${this.homeworkLines()}\n\nI'll tick each one off here as it's done.`,
      r.discord_user_id ? [r.discord_user_id] : [],
    )
  }

  // ---- PATCH /installs/:id ----------------------------------------------

  async patchSchedule(scheduledAt: number): Promise<Reply> {
    const r = this.row()
    if (!r) return { status: 404, body: { error: 'not_found' } }
    // needs_human counts as "before it starts" when no run ever began, e.g.
    // homework was still missing at the slot and the customer rebooks.
    const started = (await this.d.kv.get<number>(K.runStartedAt)) != null
    const reschedulable = r.status === 'homework' || r.status === 'scheduled' || (r.status === 'needs_human' && !started)
    if (!reschedulable) {
      return { status: 409, body: { error: `install is ${r.status}; the slot can only change before the run starts` } }
    }
    this.update({ scheduled_at: scheduledAt, status: 'scheduled' })
    await this.d.kv.delete(K.startNextCheck)
    await this.d.kv.delete(K.startMissPosted)
    await this.d.kv.delete(K.startChecks)
    await this.appendJournal('note', `slot set to ${new Date(scheduledAt * 1000).toISOString()}`)
    await this.say(r.channel_id, `Your install is booked for <t:${scheduledAt}:F>.`)
    await this.arm()
    await this.flush()
    return { status: 200, body: { install_id: r.install_id, channel_id: r.channel_id, scheduled_at: scheduledAt } }
  }

  // ---- admin -------------------------------------------------------------

  async setHomework(item: string, state: HomeworkState): Promise<Reply> {
    const r = this.row()
    if (!r) return { status: 404, body: { error: 'not_found' } }
    if (!ITEM_RE.test(item) || (state !== 'pending' && state !== 'green')) return { status: 400, body: { error: 'bad item or state' } }
    this.d.sql.exec(
      'INSERT INTO homework (install_id, item, state, updated_at) VALUES (?, ?, ?, ?) ON CONFLICT (install_id, item) DO UPDATE SET state = excluded.state, updated_at = excluded.updated_at',
      r.install_id,
      item,
      state,
      this.nowS(),
    )
    await this.appendJournal('note', `homework ${item} -> ${state}`)
    if (state === 'green') {
      const done = allGreen(this.homework().map((h) => h.state))
      await this.say(r.channel_id, `✓ ${HOMEWORK_LABELS[item] ?? item}${done ? "\n\nThat's the last one. Homework's done." : ''}`)
    }
    return { status: 200, body: { install_id: r.install_id, homework: this.homework() } }
  }

  // Stub for build step 5: the Hetzner step will call this after it creates
  // the server. Until then an admin passes the values in by hand.
  async pinServer(serverIp: string, hostKeyFp: string): Promise<Reply> {
    const r = this.row()
    if (!r) return { status: 404, body: { error: 'not_found' } }
    try {
      assertPinned({ serverIp, hostKeyFp })
    } catch (e) {
      return { status: 400, body: { error: errText(e) } }
    }
    if (r.server_ip && (r.server_ip !== serverIp || r.host_key_fp !== hostKeyFp)) {
      return { status: 409, body: { error: 'a different server is already pinned for this install' } }
    }
    this.update({ server_ip: serverIp, host_key_fp: hostKeyFp })
    await this.appendJournal('note', `server pinned: ${serverIp} ${hostKeyFp}`)
    await this.flush()
    return { status: 200, body: { install_id: r.install_id, server_ip: serverIp, host_key_fp: hostKeyFp, public_key: await this.d.kv.get<string>(K.publicKey) } }
  }

  journalView(): Reply {
    const r = this.row()
    if (!r) return { status: 404, body: { error: 'not_found' } }
    const journal = this.d.sql.exec('SELECT * FROM journal ORDER BY seq').toArray()
    return { status: 200, body: { install: r, homework: this.homework(), journal } }
  }

  // ---- the box seam ------------------------------------------------------

  // Every command on the box goes through here: the command is refused if it
  // carries a known secret, output is redacted before it is journaled or
  // returned, so the runbook agent only ever sees redacted text.
  async exec(step: number, stepName: string, attempt: number, cmd: string, opts: RunOpts = {}): Promise<RunResult> {
    const r = this.requireRow()
    if (opts.stdin) this.sessionSecrets.push(new TextDecoder().decode(opts.stdin))
    const red = await this.redactor()
    const jo = { step, stepName, attempt }
    if (red.contains(cmd)) {
      await this.appendJournal('note', 'refused a command that contained a secret value; secrets go over stdin', jo)
      throw new Error('refusing: command contains a secret value; pass it via stdin')
    }
    let transport: SshTransport
    try {
      transport = new SshTransport({
        pin: { serverIp: r.server_ip ?? undefined, hostKeyFp: r.host_key_fp ?? undefined },
        user: SSH_USER,
        privateKeyPem: (await this.d.kv.get<string>(K.privateKey)) ?? '',
        send: (req) => this.d.sendToBox(r.install_id, req),
      })
    } catch (e) {
      await this.appendJournal('note', errText(e), jo)
      throw e
    }
    await this.appendJournal('cmd', cmd, jo)
    let res: RunResult
    try {
      res = await transport.run(cmd, opts)
    } catch (e) {
      await this.appendJournal('note', `transport error: ${errText(e)}`, jo)
      throw e
    }
    const out = { code: res.code, stdout: red.redact(res.stdout), stderr: red.redact(res.stderr) }
    await this.appendJournal('result', out.stdout + (out.stderr ? `\n[stderr]\n${out.stderr}` : ''), { ...jo, exitCode: out.code })
    return out
  }

  // ---- caps --------------------------------------------------------------

  // TODO(runbook): the per-step claude -p runner calls this with each run's
  // cost. Hitting the cap escalates.
  async chargeSpend(cents: number, step: number, stepName: string): Promise<void> {
    const r = this.requireRow()
    const spend = r.spend_usd_cents + Math.max(0, Math.round(cents))
    this.update({ spend_usd_cents: spend })
    await this.appendJournal('note', `spend +${cents}c, total ${spend}c`, { step, stepName, costCents: cents })
    await this.enforceCaps(step, stepName)
    await this.flush()
  }

  private async enforceCaps(step = 0, stepName = 'caps'): Promise<boolean> {
    const r = this.requireRow()
    if (r.status !== 'running') return false
    const breach = capBreach({
      spendCents: r.spend_usd_cents,
      runStartedAt: (await this.d.kv.get<number>(K.runStartedAt)) ?? null,
      now: this.nowS(),
    })
    if (!breach) return false
    const what = breach === 'spend' ? `spend cap hit (${r.spend_usd_cents}c)` : `wall-clock cap hit (${WALL_CLOCK_CAP / HOUR}h)`
    await this.escalate(what, { step, stepName })
    return true
  }

  // ---- lifecycle ---------------------------------------------------------

  private async startRun(): Promise<void> {
    this.update({ status: 'running' })
    await this.d.kv.put(K.runStartedAt, this.nowS())
    await this.appendJournal('note', 'slot reached and homework green: run started')
    await this.say(this.requireRow().channel_id, 'Starting your install now.')
    // TODO(runbook, build step 5): hand off to the per-step `claude -p`
    // runbook agent. Until it exists, a human takes the run from here.
    await this.escalate('install is due but the runbook agent is not built yet; run this one by hand', { step: 0, stepName: 'start' })
  }

  async escalate(reason: string, o: EscalateOpts = {}): Promise<void> {
    const r = this.requireRow()
    this.update({ status: 'needs_human' })
    const step = o.step ?? 0
    const stepName = o.stepName ?? 'lifecycle'
    await this.appendJournal('escalation', reason, { step, stepName, attempt: o.attempts ?? 0 })
    await this.notifyOps(
      `**${r.install_id}** (${r.customer_name ?? 'unknown customer'}) step ${step} ${stepName} - needs_human\n${reason}\nattempts: ${o.attempts ?? 'n/a'}\njournal: ${await this.journalUrl()}`,
    )
    await this.say(
      r.channel_id,
      o.customerNeeded
        ? 'The install is on hold until the homework above is done. Someone from the team will follow up here.'
        : "I'm stuck on something and need a person to look at it. Nothing for you to do. Someone from the team will pick it up here.",
    )
  }

  private async notifyOps(text: string): Promise<void> {
    await this.say(this.d.env.DISCORD_ESCALATION_CHANNEL_ID, text)
  }

  private async journalUrl(): Promise<string> {
    const origin = (await this.d.kv.get<string>(K.origin)) ?? ''
    return `${origin}/installs/${this.requireRow().install_id}/journal`
  }

  // TODO(runbook): called by the runbook agent when the last step passes or
  // it gives up. Starts the GC clock.
  async finish(status: Extract<InstallStatus, 'done' | 'failed'>): Promise<void> {
    this.update({ status })
    await this.d.kv.put(K.gcEligibleSince, this.nowS())
    await this.appendJournal('note', `install ${status}`)
    await this.arm()
    await this.flush()
  }

  // Admin stand-in for the runbook agent's finish (POST /installs/:id/finish),
  // so a hand-run install can enter GC and eventually close.
  async finishByAdmin(status: Extract<InstallStatus, 'done' | 'failed'>): Promise<Reply> {
    const r = this.row()
    if (!r) return { status: 404, body: { error: 'not_found' } }
    if (r.status === 'closed') return { status: 409, body: { error: 'install is closed' } }
    await this.finish(status)
    const after = this.requireRow()
    return { status: 200, body: { install_id: after.install_id, status: after.status } }
  }

  // ---- alarm -------------------------------------------------------------

  async onAlarm(): Promise<void> {
    const r = this.row()
    if (!r || r.status === 'closed') {
      await this.d.setAlarm(null)
      return
    }
    const now = this.nowS()
    if (r.status === 'scheduled' && r.scheduled_at !== null) await this.applyStartRule(r, now)
    else if (r.status === 'running') await this.enforceCaps()
    else if (r.status === 'done' || r.status === 'failed') await this.applyGc(now)
    await this.arm()
    await this.flush()
  }

  private async applyStartRule(r: InstallRow, now: number): Promise<void> {
    const hw = this.homework()
    const d = startDecision({ now, scheduledAt: r.scheduled_at as number, homework: hw.map((h) => h.state) })
    if (d.action === 'wait_slot') return
    await this.d.kv.delete(K.startNextCheck)
    if (d.action === 'start') {
      await this.startRun()
      return
    }
    const checks = ((await this.d.kv.get<number>(K.startChecks)) ?? 0) + 1
    await this.d.kv.put(K.startChecks, checks)
    const missing = hw.filter((h) => h.state !== 'green').map((h) => HOMEWORK_LABELS[h.item] ?? h.item)
    if (d.action === 'escalate') {
      await this.escalate(`homework still not green 2h after the slot: ${missing.join(', ')}`, {
        step: 0,
        stepName: 'start',
        attempts: checks,
        customerNeeded: true,
      })
      return
    }
    await this.d.kv.put(K.startNextCheck, d.next)
    await this.appendJournal('customer_wait', `slot reached, waiting on homework: ${missing.join(', ')}`, { stepName: 'start', attempt: checks })
    if (!(await this.d.kv.get<boolean>(K.startMissPosted))) {
      await this.d.kv.put(K.startMissPosted, true)
      await this.say(
        r.channel_id,
        `It's install time, but these still need doing:\n${missing.map((m) => `○ ${m}`).join('\n')}\n\nI'll check again every 15 minutes.`,
      )
    }
  }

  private async gcEligibleSince(r: InstallRow): Promise<number> {
    return (await this.d.kv.get<number>(K.gcEligibleSince)) ?? r.updated_at
  }

  private async applyGc(now: number): Promise<void> {
    let r = this.requireRow()
    if (r.channel_id && r.discord_user_id) {
      try {
        const latest = await this.discord().latestMessageAt(r.channel_id, r.discord_user_id)
        if (latest !== null && latest > (r.last_customer_msg_at ?? 0)) {
          const fields: Partial<InstallRow> = { last_customer_msg_at: latest }
          // Any customer message resets the clock, including after a warning.
          if (r.close_warned_at !== null && latest > r.close_warned_at) fields.close_warned_at = null
          this.update(fields)
          r = this.requireRow()
        }
      } catch (e) {
        // Never close on a clock we could not refresh. Try again in an hour.
        await this.appendJournal('note', `gc: could not read channel, not closing: ${errText(e)}`, { stepName: 'gc' })
        await this.d.kv.put(K.gcRetryAfter, now + HOUR)
        return
      }
    }
    await this.d.kv.delete(K.gcRetryAfter)
    const d = gcDecision({ now, base: gcBase(r.last_customer_msg_at, await this.gcEligibleSince(r)), warnedAt: r.close_warned_at })
    if (d.action === 'warn') {
      this.update({ close_warned_at: now })
      await this.appendJournal('note', `gc: warned, closing at ${new Date(d.closeAt * 1000).toISOString()} unless the customer posts`, { stepName: 'gc' })
      await this.say(
        r.channel_id,
        `This channel closes <t:${d.closeAt}:R> if it stays quiet. If anything about your bot seems off, say so here and the clock resets.`,
      )
    } else if (d.action === 'close') {
      await this.close('14 days without a customer message')
    }
  }

  private async arm(): Promise<void> {
    const r = this.row()
    const now = this.nowS()
    let next: number | null = null
    if (r && r.status === 'scheduled' && r.scheduled_at !== null) {
      next = (await this.d.kv.get<number>(K.startNextCheck)) ?? r.scheduled_at
    } else if (r && r.status === 'running') {
      const started = await this.d.kv.get<number>(K.runStartedAt)
      next = started != null ? started + WALL_CLOCK_CAP : null
    } else if (r && (r.status === 'done' || r.status === 'failed')) {
      const retry = await this.d.kv.get<number>(K.gcRetryAfter)
      const d = gcDecision({ now, base: gcBase(r.last_customer_msg_at, await this.gcEligibleSince(r)), warnedAt: r.close_warned_at })
      next = Math.max(d.action === 'none' ? d.next : now, retry ?? 0)
    }
    await this.d.setAlarm(next === null ? null : Math.max(next, now) * 1000)
  }

  // Hard requirements 2 and 7. Removes our key from the box and from Worker
  // storage. It never touches the customer's server itself: there is no
  // Hetzner call anywhere in this Worker.
  async close(reason: string): Promise<void> {
    const r = this.requireRow()
    if (r.status === 'closed') return
    await this.appendJournal('note', `closing: ${reason}`, { stepName: 'close' })
    const priv = await this.d.kv.get<string>(K.privateKey)
    if (r.server_ip && r.host_key_fp && priv) {
      let failure: string | null = null
      try {
        const pub = (await this.d.kv.get<string>(K.publicKey)) ?? ''
        const res = await this.exec(0, 'close', 1, removeKeyCommand(pub))
        if (res.code !== 0) failure = `exit ${res.code}`
      } catch (e) {
        failure = errText(e)
      }
      if (failure) {
        await this.notifyOps(
          `**${r.install_id}** close: could not remove the installer key from ${r.server_ip} (${failure}). The private half is deleted anyway; remove the \`${keyComment(r.install_id)}\` line from authorized_keys by hand.`,
        )
      }
    } else {
      await this.appendJournal('note', 'no server pinned; no key to remove from a box', { stepName: 'close' })
    }
    await this.d.kv.delete(K.privateKey)
    await this.d.kv.delete(K.hetznerToken)
    await this.appendJournal('note', 'install key and any Hetzner token deleted from Worker storage', { stepName: 'close' })
    if (r.channel_id) {
      try {
        await this.discord().deleteChannel(r.channel_id)
      } catch (e) {
        await this.appendJournal('note', `channel delete failed: ${errText(e)}`, { stepName: 'close' })
        await this.notifyOps(`**${r.install_id}** close: could not delete channel ${r.channel_id}; delete it by hand.`)
      }
    }
    this.update({ status: 'closed' })
    await this.d.setAlarm(null)
    await this.flush()
  }
}

// Drops every authorized_keys line carrying our key blob, then fails unless
// the blob is gone, so "no matching line" can never read as success when the
// key is still there. `cat >` keeps the file's owner and mode.
export function removeKeyCommand(publicKey: string): string {
  const blob = publicKey.split(' ')[1] ?? ''
  if (!/^AAAAC3NzaC1lZDI1NTE5[A-Za-z0-9+/]+=*$/.test(blob)) throw new Error('no installer public key on record')
  const f = '"$HOME/.ssh/authorized_keys"'
  return `test -f ${f} || exit 0; grep -vF '${blob}' ${f} > ${f}.tmp; cat ${f}.tmp > ${f} && rm -f ${f}.tmp && ! grep -qF '${blob}' ${f}`
}

function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}
