// The relay this build talks to. build.sh rewrites this line and the
// manifest's host_permissions together when RELAY_ORIGIN is set, so a local
// build can point at `wrangler dev`.
export const RELAY_ORIGIN = 'https://behalfbot-installer.sean-8a1.workers.dev'
