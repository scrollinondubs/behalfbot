# Memory Seeding (install day)

The chassis ships memory **pre-seeded** for new installers — not empty. Per a 2026-05-05 design call: time-to-delight matters. An installer opening their first session with `lead:<top-3-prospects>`, `topic:<their-active-research-area>`, `feedback:<their-communication-preferences>` already there means they get useful behavior from minute one rather than after weeks of incremental memory accumulation.

This document covers what gets seeded, where the inputs come from, and the no-fabrication rule.

## Source

Two inputs feed the seed:

1. **`INSTALL_PROFILE.md`** — narrative artifact from the install interview. Captures bio, focus area, communication style, modules wanted, trust line, etc.
2. **`chassis.config.yaml`** — machine-readable companion. Tells us which plugins to seed memory for (e.g. only seed `lead:*` entries if the dating or sdr-outreach modules are enabled).

The bootstrap script reads both, generates seed memory entries, drops them into `${CHASSIS_HOME}/memory/`, and updates `${CHASSIS_HOME}/memory/MEMORY.md` to index them.

## Seed entries (typical)

For every install, the bootstrap generates at minimum:

| File | Source | Notes |
|---|---|---|
| `user_<installer>_bio.md` | INSTALL_PROFILE.md identity + scope sections | Bio, role, machine, focus area, pricing intent |
| `user_<installer>_communication_style.md` | INSTALL_PROFILE.md tone section | Reply length preference, key phrases that mean specific things |
| `feedback_<installer>_voice.md` | INSTALL_PROFILE.md outreach + voice sections | How to write in their voice (banned words, structural patterns) |
| `reference_emergency_contacts.md` | INSTALL_PROFILE.md (if Angel Protocol enabled) | Gitignored — local only |

Plus per-plugin:

| Plugin | Seed entries |
|---|---|
| `dating` | `feedback_dating_calibration_<installer>.md` (taste refs, hard rules), `feedback_dating_concierge_framing.md` (chassis-default), `lead:*` for any pre-existing matches the installer wants tracked from day one |
| `bfl` | `feedback_bfl_macros_<installer>.md` (daily protein/carb targets), `feedback_bfl_notebook_quirks.md` (year hallucination override, meal-time jumbling) |
| `crm` (Notion) | `topic_<installer>_lp_pipeline.md` (their fund's investment thesis), `feedback_notion_para_conventions.md` (PARA mirror) |
| `sdr-outreach` | `feedback_outreach_voice_<installer>.md` (cohort-pitch tone, signoff style) |

Plus chassis-core defaults applicable to every install:

| File | Notes |
|---|---|
| `feedback_never_deceive.md` | Never fabricate data/rationale/outcomes |
| `feedback_never_commit_coords.md` | Lat/lng coords local-only (already enforced at hook layer; memory entry is for reasoning) |
| `feedback_humanize_copy.md` | Brand voice → /humanizer for any public-facing copy |
| `feedback_no_em_dash.md` | Use space-dash-space; em dashes are an AI tell |

## The no-fabrication rule

**Only seed what the installer told us, OR what's directly observable from their existing tooling.** Never infer.

Examples of valid seeds:
- Installer said in interview: "I prefer terse replies, 3-5 sentences max" → ship as `user_<installer>_communication_style.md`
- Installer's Notion has a database called "LP Pipeline" → ship as `reference_lp_pipeline.md` pointing at that database
- Installer's GitHub shows them as a maintainer of repo X → ship as `reference_<repo>_maintainer.md`

Examples of INVALID seeds (don't fabricate):
- "Installer probably likes emoji because they're millennial" → no
- "Installer probably wants the dating module configured for serious relationships because they mentioned wanting to settle down" → no, ask them directly at install time
- Inferring tone from their public Twitter posts → only if the installer explicitly OK'd that as input

When in doubt, leave it out. Better an installer adds a memory entry the second time something comes up than an agent acting on a fabricated assumption.

## How the bootstrap script seeds

Pseudocode:

```python
profile = parse_install_profile_md(install_profile_path)
config = parse_chassis_config_yaml(config_path)

# Always-on seeds (chassis-core defaults)
write_memory_file("feedback_never_deceive.md", chassis_default_content("never_deceive"))
write_memory_file("feedback_never_commit_coords.md", chassis_default_content("never_commit_coords"))
# ... etc

# Installer-derived seeds
write_memory_file(f"user_{slug}_bio.md", render_bio_from_profile(profile))
write_memory_file(f"user_{slug}_communication_style.md", render_style_from_profile(profile))

# Per-plugin seeds
for module, settings in config.modules.items():
    if not settings.enabled:
        continue
    if module == "dating":
        write_memory_file(f"feedback_dating_calibration_{slug}.md", profile.dating_calibration)
        # Cleared-matches.json template gets dropped in plugins/dating/, NOT memory/
    if module == "bfl":
        write_memory_file(f"feedback_bfl_macros_{slug}.md", profile.bfl_macros)
    # ... etc

# Update MEMORY.md index with one line per seeded file
update_memory_index(memory_dir)

print(f"Seeded {len(seeded_files)} memory entries.")
```

## What does NOT get seeded

- **Lead state** beyond what the installer explicitly named at install. Real lead memories accumulate via the dating / sdr-outreach plugins as the installer interacts with them.
- **Project state** of any kind — projects evolve fast and the agent picks them up via `gh issue list`, not via stale memory.
- **Task memories** — those are written as the agent completes work, not pre-seeded.
- **References to systems the agent can't actually reach** (e.g. "ask installer about their Notion" when no Notion integration is wired).

## Verification at install time

After seeding, the bootstrap script runs a sanity check:

1. Every file in `${CHASSIS_HOME}/memory/` has valid frontmatter (`name`, `description`, `type`)
2. Every file has a corresponding entry in `MEMORY.md`
3. Every entry in `MEMORY.md` points to an existing file
4. No file references a `${PLACEHOLDER}` value that wasn't filled

Failures here block install completion — better to catch a malformed seed than ship it and wonder why memory recall is broken.

## Cross-references

- `chassis/memory/MEMORY.md.template` — empty index template
- `chassis/memory/WRITING_RULES.md` — full type taxonomy + body structure rules
- `chassis/memory/template_*.md` — per-type frontmatter examples
- `INSTALL_PROFILE.md` (per installer) — narrative source for bio + voice + module-calibration seeds
- `chassis.config.yaml` (per installer) — machine-readable source for which plugins seed which entries
