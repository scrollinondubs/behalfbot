// Deep health check (issue #173). Plain /healthz answers from the Worker's
// own APP_COMMIT var, which deploy.sh sets during the Worker upload - before
// wrangler builds and pushes the container image. When that push fails the
// Worker already reports the new commit while the container still runs the
// old image (2026-10-22, a registry "broken pipe"). Only asking the container
// itself tells those two apart.

export interface BuildIdentity {
  appCommit: string | null
  builtAt: string | null
}

export interface ContainerIdentity extends BuildIdentity {
  inFlight: boolean | null
}

function str(v: unknown): string | null {
  return typeof v === 'string' && v ? v : null
}

export async function deepHealthz(
  worker: BuildIdentity,
  probe: () => Promise<Response>,
): Promise<Response> {
  let container: ContainerIdentity | null = null
  let containerError: string | null = null
  try {
    const res = await probe()
    if (!res.ok) {
      containerError = `container /healthz answered HTTP ${res.status}`
    } else {
      const body = (await res.json()) as Record<string, unknown>
      container = {
        appCommit: str(body.appCommit),
        builtAt: str(body.builtAt),
        inFlight: typeof body.inFlight === 'boolean' ? body.inFlight : null,
      }
    }
  } catch (e) {
    containerError = e instanceof Error ? e.message : String(e)
  }

  const inSync =
    container !== null && worker.appCommit !== null && container.appCommit === worker.appCommit

  return Response.json(
    {
      ok: container !== null,
      appCommit: worker.appCommit,
      builtAt: worker.builtAt,
      worker,
      container,
      containerError,
      inSync,
    },
    { status: container !== null ? 200 : 503 },
  )
}
