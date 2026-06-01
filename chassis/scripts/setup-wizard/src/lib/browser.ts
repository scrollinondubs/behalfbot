import { chromium, type Browser, type BrowserContext, type Page } from "playwright";
import { existsSync } from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";

export interface BrowserSession {
  browser: Browser;
  context: BrowserContext;
  page: Page;
}

const STORAGE_STATE_DIR = join(homedir(), ".behalf-bot-wizard");

export function storageStatePath(service: string): string {
  return join(STORAGE_STATE_DIR, `${service}-session.json`);
}

export async function launchSession(
  service: string,
  freshLogin: boolean,
  dryRun: boolean
): Promise<BrowserSession | null> {
  if (dryRun) {
    console.log(
      `[dry-run] would launch visible browser for ${service} (storage-state: ${storageStatePath(service)})`
    );
    return null;
  }

  const browser = await chromium.launch({ headless: false });
  const statePath = storageStatePath(service);
  const hasExistingSession = !freshLogin && existsSync(statePath);

  const context = hasExistingSession
    ? await browser.newContext({ storageState: statePath })
    : await browser.newContext();

  if (hasExistingSession) {
    console.log(
      `  Reusing existing ${service} session (pass --fresh-login to sign in again).`
    );
  }

  const page = await context.newPage();
  return { browser, context, page };
}

export async function saveSession(
  session: BrowserSession,
  service: string,
  dryRun: boolean
): Promise<void> {
  if (dryRun) return;
  const { mkdirSync } = await import("node:fs");
  mkdirSync(STORAGE_STATE_DIR, { recursive: true });
  await session.context.storageState({ path: storageStatePath(service) });
}

export async function closeSession(session: BrowserSession): Promise<void> {
  await session.context.close();
  await session.browser.close();
}
