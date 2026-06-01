import prompts from "prompts";
import type { Page } from "playwright";
import { launchSession, saveSession, closeSession } from "../lib/browser.js";
import { writeVwItem } from "../lib/vw.js";

export interface NotionResult {
  integrationToken: string;
  databases: Record<string, string>;
}

interface FlowOptions {
  installerName: string;
  freshLogin: boolean;
  dryRun: boolean;
}

export async function runNotionFlow(opts: FlowOptions): Promise<NotionResult> {
  console.log("\n--- Notion flow ---");

  if (opts.dryRun) {
    console.log("[dry-run] would open https://www.notion.so/my-integrations");
    console.log("[dry-run] would create integration named: Behalf.bot -", opts.installerName);
    console.log(
      '[dry-run] would capture token and write VW item "Behalf.bot - Notion integration token"'
    );
    console.log(
      "[dry-run] would prompt installer for DB/page URLs and write VW item \"Behalf.bot - Notion DB IDs\""
    );
    return {
      integrationToken: "ntn_DRY_RUN_TOKEN",
      databases: {
        NOTION_PROJECT_TRACKER_DB_ID: "00000000000000000000000000000001",
        NOTION_READING_LIST_DB_ID: "00000000000000000000000000000002",
        NOTION_MEMORY_PAGE_ID: "00000000000000000000000000000003",
      },
    };
  }

  const session = await launchSession("notion", opts.freshLogin, opts.dryRun);
  if (!session) throw new Error("session should exist in wet mode");

  const { page } = session;
  let integrationToken = "";
  const databases: Record<string, string> = {};

  try {
    integrationToken = await createNotionIntegration(page, opts.installerName);

    console.log("  Notion integration token captured.");

    writeVwItem(
      {
        name: "Behalf.bot - Notion integration token",
        password: integrationToken,
      },
      opts.dryRun
    );

    console.log('  Wrote VW item "Behalf.bot - Notion integration token".');

    const dbIds = await promptAndShareDatabases(page, opts);
    Object.assign(databases, dbIds);

    const dbNotes = Object.entries(databases)
      .map(([k, v]) => `${k}=${v}`)
      .join("\n");

    writeVwItem(
      {
        name: "Behalf.bot - Notion DB IDs",
        notes: dbNotes,
      },
      opts.dryRun
    );

    console.log('  Wrote VW item "Behalf.bot - Notion DB IDs".');

    await saveSession(session, "notion", opts.dryRun);
  } finally {
    await closeSession(session);
  }

  return { integrationToken, databases };
}

async function createNotionIntegration(
  page: Page,
  installerName: string
): Promise<string> {
  await page.goto("https://www.notion.so/my-integrations");

  // If redirected to login, wait for the installer to sign in manually.
  // The wizard does NOT automate the credential entry - installer signs in
  // themselves in the visible browser window.
  const isLoggedIn = await waitForNotionLogin(page);
  if (!isLoggedIn) {
    throw new Error("Notion login timed out (5 min). Restart the wizard and try again.");
  }

  // Click "New integration"
  await page.getByRole("button", { name: /new integration/i }).click();

  // Fill integration name
  const integrationName = `Behalf.bot - ${installerName}`;
  const nameInput = page.getByLabel(/name/i).first();
  await nameInput.fill(integrationName);

  // Set capability scopes: Read, Update, Insert content
  // Notion's integration scopes page uses checkboxes; confirm content capabilities
  // are checked by default (they are as of 2026) and User Info is not needed.
  // We just confirm and submit.
  await page.getByRole("button", { name: /submit/i }).click();

  // The token is shown on the "Secrets" section of the integration detail page.
  // It starts with "ntn_" or "secret_". Click "Show" then copy.
  await page.waitForSelector('[data-testid="integration-secret"]', {
    timeout: 15_000,
  }).catch(() => null);

  // Try the show/reveal button
  const showButton = page.getByRole("button", { name: /show/i }).first();
  if (await showButton.isVisible()) {
    await showButton.click();
  }

  // Extract token from input field
  const tokenInput = page.locator('input[type="text"][value^="ntn_"], input[type="text"][value^="secret_"]').first();
  const token = await tokenInput.inputValue().catch(() => "");

  if (!token) {
    // Fallback: prompt installer to paste it manually if selector fails
    console.warn(
      "  Could not auto-capture the Notion integration token from the page."
    );
    console.warn(
      "  Please copy the token from the browser and paste it here."
    );
    const { manualToken } = await prompts({
      type: "password",
      name: "manualToken",
      message: "Paste the Notion integration token:",
    });
    return manualToken as string;
  }

  return token;
}

async function waitForNotionLogin(page: Page): Promise<boolean> {
  // Either we're already on the integrations page, or we need to wait for login.
  const fiveMinutes = 5 * 60 * 1000;
  const start = Date.now();

  while (Date.now() - start < fiveMinutes) {
    const url = page.url();
    // After login, Notion redirects to notion.so/... (not /login)
    if (url.includes("notion.so") && !url.includes("/login")) {
      return true;
    }
    // Also accept if we can see the integrations UI directly
    const hasIntegrations = await page
      .getByRole("button", { name: /new integration/i })
      .isVisible()
      .catch(() => false);
    if (hasIntegrations) return true;

    console.log("  Waiting for Notion login in browser window...");
    await page.waitForTimeout(5_000);
  }
  return false;
}

async function promptAndShareDatabases(
  page: Page,
  opts: FlowOptions
): Promise<Record<string, string>> {
  console.log("\n  Now connect your Notion databases to this integration.");
  console.log(
    "  For each database or page you want Behalf.bot to access, paste its URL below."
  );
  console.log(
    "  The wizard will open the page and click 'Add Connection' automatically.\n"
  );

  const dbKeys = [
    { key: "NOTION_PROJECT_TRACKER_DB_ID", label: "Project Tracker database URL (or press Enter to skip)" },
    { key: "NOTION_READING_LIST_DB_ID", label: "Reading List database URL (or press Enter to skip)" },
    { key: "NOTION_MEMORY_PAGE_ID", label: "Memory/Notes page URL (or press Enter to skip)" },
  ];

  const result: Record<string, string> = {};

  for (const { key, label } of dbKeys) {
    const { url: dbUrl } = await prompts({
      type: "text",
      name: "url",
      message: label,
    });

    if (!dbUrl) continue;

    const id = await sharePageWithIntegration(page, dbUrl, opts);
    if (id) {
      result[key] = id;
      console.log(`  ${key} = ${id}`);
    }
  }

  // Allow additional databases beyond the standard three
  let addMore = true;
  let extraIndex = 1;
  while (addMore) {
    const { extraUrl } = await prompts({
      type: "text",
      name: "extraUrl",
      message: `Additional page URL (or press Enter when done):`,
    });

    if (!extraUrl) {
      addMore = false;
      break;
    }

    const { extraKey } = await prompts({
      type: "text",
      name: "extraKey",
      message: `Env var name for this page (e.g. NOTION_TASKS_DB_ID):`,
    });

    if (!extraKey) continue;

    const id = await sharePageWithIntegration(page, extraUrl, opts);
    if (id) {
      result[extraKey] = id;
      console.log(`  ${extraKey} = ${id}`);
    }
    extraIndex++;
  }

  return result;
}

async function sharePageWithIntegration(
  page: Page,
  pageUrl: string,
  opts: FlowOptions
): Promise<string | null> {
  if (opts.dryRun) {
    console.log(`[dry-run] would navigate to ${pageUrl} and click Add Connection`);
    return "00000000000000000000000000000000";
  }

  await page.goto(pageUrl);

  // Click the "..." or "Share" menu to find "Add connections"
  // Notion's share button is in the top-right toolbar
  const shareButton = page.getByRole("button", { name: /share/i }).first();
  await shareButton.click().catch(() => null);

  // Look for "Add connections" or "Connect to" option
  const addConnectionsBtn = page.getByText(/add connections|connect to/i).first();
  await addConnectionsBtn.click().catch(async () => {
    // Fallback: try the "..." menu
    await page.getByRole("button", { name: /\.\.\./i }).first().click().catch(() => null);
  });

  // Search for the integration by name
  const integrationName = `Behalf.bot - ${opts.installerName}`;
  const searchInput = page.getByPlaceholder(/search/i).first();
  await searchInput.fill(integrationName).catch(() => null);

  // Click the integration in the dropdown
  const integrationOption = page.getByText(integrationName, { exact: false }).first();
  await integrationOption.click().catch(async () => {
    console.warn(
      `  Could not auto-click "Add Connection" for ${pageUrl}.`
    );
    console.warn(
      "  Please manually: open that page in Notion, click Share, click 'Add connections', select your integration."
    );
    console.warn("  Press Enter in this terminal when done.");
    await prompts({ type: "text", name: "_", message: "Press Enter to continue..." });
  });

  // Extract the page ID from the URL
  // Notion page URLs end in: notion.so/<workspace>/<page-title>-<32-char-hex-id>
  // or notion.so/<32-char-hex-id>
  const finalUrl = page.url();
  const idMatch = finalUrl.match(/([0-9a-f]{32})(?:\?|$)/);
  if (!idMatch) {
    // Try shorter format
    const shortMatch = finalUrl.match(/[?&]p=([0-9a-f-]{36})/);
    if (shortMatch) return shortMatch[1].replace(/-/g, "");
    console.warn(`  Could not extract page ID from URL: ${finalUrl}`);
    return null;
  }

  return idMatch[1];
}
