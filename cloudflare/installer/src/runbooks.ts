// Runbooks the browser autopilot can pair for. Each one fixes the sites the
// extension may touch for that task; the code carries only the runbook id.

import googlePlaces from '../../../runbooks/google-places.json' with { type: 'json' }
import localTest from '../../../runbooks/local-test.json' with { type: 'json' }
import awsBackupHardening from '../../../runbooks/aws-backup-hardening.json' with { type: 'json' }
import stripeGoldForSilver from '../../../runbooks/stripe-gold-for-silver.json' with { type: 'json' }

export interface Runbook {
  id: string
  task: string
  allow: string[]
  start_url: string
  // Only pairable when the Worker runs with AUTOPILOT_DEV_RUNBOOKS=1.
  dev_only?: boolean
  steps: unknown[]
}

const ALL: Runbook[] = [
  googlePlaces as Runbook,
  localTest as Runbook,
  awsBackupHardening as Runbook,
  stripeGoldForSilver as Runbook,
]

export function runbook(id: string): Runbook | null {
  return ALL.find((r) => r.id === id) ?? null
}
