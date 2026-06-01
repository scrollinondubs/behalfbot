// TODO: Phase 2 - Oura Personal Access Token provisioning
//
// Scope (from issue #29 Phase 2):
//   - Navigate to cloud.ouraring.com
//   - Sign in (visible browser, installer authenticates manually)
//   - Navigate to Personal Access Tokens page
//   - Click "Create New Personal Access Token"
//   - Name it "Behalf.bot - <InstallerName>"
//   - Copy the generated token to VW item "Behalf.bot - Oura PAT"
//
// VW item: "Behalf.bot - Oura PAT" (password field) -> OURA_PERSONAL_ACCESS_TOKEN
// Canonical item name defined in docs/installer-vw-template.md Section 5.
//
// Skip condition: if installer is not on Oura Ring, this flow is skipped entirely.
// The chassis BFL plugin degrades gracefully when OURA_PERSONAL_ACCESS_TOKEN is absent
// (Strava HR data is used as the fallback).

export {};
