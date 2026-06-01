// TODO: Phase 2 - FDC API key provisioning
//
// Scope (from issue #29 Phase 2):
//   - Navigate to api.nal.usda.gov/fdc
//   - Fill the signup form with installer's name + email
//   - Notify installer to confirm the FDC confirmation email
//   - If installer grants Gmail access (via the Google flow's token), poll
//     their inbox for the confirmation link and auto-click it
//   - Copy the API key to VW item "Behalf.bot - FDC API key"
//
// VW item: "Behalf.bot - FDC API key" (password field) -> USDA_FDC_API_KEY
// Canonical item name defined in docs/installer-vw-template.md Section 5.
//
// Note: DEMO_KEY works at low volume. The wizard should offer to skip FDC
// and use DEMO_KEY if the installer is just testing.

export {};
