# Behalf.bot INSTALL_PROFILE - Installer Template

> **This is a template.** Do NOT fill in real values here for a real install.
> Instead: create a customer/<installer> branch off main, copy this file to
> `docs/install-<installer>-profile.md`, and fill in real values there.
> See `customer/installer-2` and `customer/installer-1` for reference examples.
>
> **For V1 case studies:** profiles were hand-authored from in-person interviews.
> For V2+ installs, the Confabulator-fork agent-branch interview generates these
> artifacts conversationally. Until that ship, use this template + an interview.

---

## Why this document exists

The chassis = the core set of stuff identical across every Behalf.bot installation. The install path = a per-installer artifact (this doc + `chassis.config.yaml`) that the chassis hydrates from at bootstrap to produce a working personal AI assistant.

For V1 case studies we hand-author the artifacts directly from an interview. Once a few installs work, we reverse-engineer the interview spec from what we had to ask. **V1 installs are the empirical spec.**

---

## 1. Identity + scope

| Field | Value |
|---|---|
| `installer_name` | `<installer-name>` |
| `installer_email` | (out-of-band - populated from Vaultwarden share, not stored in this doc) |
| `instance_branch` | `agent` (per the Confabulator-fork's eventual Q1 split - agent vs app) |
| `relationship_to_sean` | `<describe relationship>` |
| `pricing_intent` | `<USD/month signal from interview>` |
| `case_study_position` | `<#N>` |

---

## 2. Target environment

| Field | Value |
|---|---|
| `target_runtime` | `<linux_baremetal \| cloudflare_containers \| mac_mini>` |
| `host_class` | `<home_server \| vps \| cloud_container>` |
| `state_storage` | `local-fs` (recommended for V1) |
| `state_path` | `~/behalfbot/state` |
| `database` | **Postgres** (chassis default; see LESSONS_FROM_V1.md #34) |
| `network` | `<tailscale_node_share \| cloudflare_tunnel \| direct_ssh>` |

---

## 3. Channels

| Field | Value |
|---|---|
| `primary_channel` | `<discord \| telegram \| slack>` |
| `secondary_channel` | `<describe or "none">` |
| `notification_strategy` | `<describe channel topology>` |

---

## 4. Tool integrations

| Tool | Role | Trust direction |
|---|---|---|
| Gmail | read all + draft | read+draft, NEVER auto-send |
| Google Calendar | read + write | read+write |
| `<second-brain>` | notes + CRM | read+write |
| Discord | primary channel | read+write |
| `<other tools>` | `<role>` | `<trust level>` |

---

## 5. Use cases (priority order from interview)

### 5a. `<Priority 1 use case>`

- `<detail>`

### 5b. `<Priority 2 use case>`

- `<detail>`

---

## 6. Customization modules

| Module | Enable? | Notes |
|---|---|---|
| `admin` | yes | rescheduling, travel, event research, email drafting |
| `briefing` | yes | daily digest |
| `crm` | `<yes/no>` | `<second-brain>` CRM integration |
| `outreach` | `<yes/no>` | `<scope>` |
| `dating` | `<yes/no>` | requires explicit opt-in + Android emulator |
| `bfl` | `<yes/no>` | Body for Life pipeline; confirm installer is doing BFL |
| `dealflow` | no | V2 candidate |
| `banking` | no | explicit trust-line opt-in required |

---

## 7. Second-brain backend

| Field | Value |
|---|---|
| `second_brain_backend` | `<notion \| siyuan \| obsidian>` |
| `workspace_or_notebook` | `<installer's workspace id or notebook path>` |
| `organization_pattern` | `<PARA \| flat \| other>` |
| `databases` | TBD on install day - at minimum: primary notes area + memory page |

---

## 8. Identity isolation + secrets

All agent-side accounts are owned by the installer. Sean+${ASSISTANT_NAME} never hold installer credentials.

| Account | Created by | Owned by |
|---|---|---|
| `<installer-agent>@<domain>` Google Workspace user | installer | installer |
| GitHub user `<installer-agent>` | installer | installer |
| Discord bot + server | installer | installer |
| Vaultwarden instance | installer (recommended) | installer |

---

## 9. V1 install procedure

Execution model: **(b) Sean+${ASSISTANT_NAME} drive via SSH** (see <v1-reference-install> #494 for the rationale). Installer provisions accounts + Linux box + Tailscale share; we do everything else. Every command we execute is logged into `bootstrap.sh` so the next installer can run a near-identical transcript.

1. Installer completes homework (`docs/installer-homework-<installer>.md`)
2. We SSH in via Tailscale-shared node
3. We install Linux prereqs
4. We clone the chassis repo to `~/behalfbot/`
5. We hydrate the chassis from this profile + `docs/install-<installer>-chassis-config.yaml`
6. We boot chassis + run smoke tests
7. First-heartbeat smoke test: 3 consecutive clean morning briefings = signed off
8. Sign-off: installer chooses ownership transfer vs ongoing DevOps shadow

---

## 10. Memory pre-seeding

Memory does NOT ship empty. Pre-seed with what came from the interview to reduce time-to-delight.

- `user_<installer>_bio.md` - key facts from interview
- `feedback_<second_brain>_conventions.md` - workspace structure discovered at install
- `feedback_dating_calibration_<installer>.md` - if dating plugin enabled
- `reference_emergency_contacts.md` - installer sets their own list (gitignored, local-only)

No fabrication. Only what installer confirms or what's directly observable from their existing tooling.

---

## 11. Open questions (to resolve at install kickoff)

- `<list open questions from interview>`

---

## 12. What this profile is NOT

- Not a finalized chassis spec - chassis design lives in `chassis/`
- Not another installer's profile - each installer gets their own
- Not a binding commitment to installer - installer validates at kickoff and can override anything
- Not the user-facing onboarding flow - that's the eventual Confabulator-fork's job

---

*Template generated from V1 install experience. Customize on your customer/<installer> branch. Sean ratifies before any chassis code reads from this.*
