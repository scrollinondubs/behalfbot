// Runbooks the browser autopilot can pair for. Each one fixes the sites the
// extension may touch for that task; the code carries only the runbook id.

import googlePlaces from '../../../runbooks/google-places.json' with { type: 'json' }
import localTest from '../../../runbooks/local-test.json' with { type: 'json' }
import awsBackupHardening from '../../../runbooks/aws-backup-hardening.json' with { type: 'json' }
import stripeGoldForSilver from '../../../runbooks/stripe-gold-for-silver.json' with { type: 'json' }
import hetznerRescale from '../../../runbooks/hetzner-rescale.json' with { type: 'json' }
import loomLibraryPurge from '../../../runbooks/loom-library-purge.json' with { type: 'json' }
import vaultwardenOrgForAgent from '../../../runbooks/vaultwarden-org-for-agent.json' with { type: 'json' }
import braveSearchApiKey from '../../../runbooks/brave-search-api-key.json' with { type: 'json' }
import githubSignupForAgent from '../../../runbooks/github-signup-for-agent.json' with { type: 'json' }
import linkedinConnectionsExport from '../../../runbooks/linkedin-connections-export.json' with { type: 'json' }
import googleContactsExport from '../../../runbooks/google-contacts-export.json' with { type: 'json' }
import outlookContactsExport from '../../../runbooks/outlook-contacts-export.json' with { type: 'json' }
import icloudContactsExport from '../../../runbooks/icloud-contacts-export.json' with { type: 'json' }
import anthropicAdminKey from '../../../runbooks/anthropic-admin-key.json' with { type: 'json' }
import podiaCapture from '../../../runbooks/podia-capture.json' with { type: 'json' }

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
  hetznerRescale as Runbook,
  loomLibraryPurge as Runbook,
  vaultwardenOrgForAgent as Runbook,
  braveSearchApiKey as Runbook,
  githubSignupForAgent as Runbook,
  linkedinConnectionsExport as Runbook,
  googleContactsExport as Runbook,
  outlookContactsExport as Runbook,
  icloudContactsExport as Runbook,
  anthropicAdminKey as Runbook,
  podiaCapture as Runbook,
]

export function runbook(id: string): Runbook | null {
  return ALL.find((r) => r.id === id) ?? null
}
