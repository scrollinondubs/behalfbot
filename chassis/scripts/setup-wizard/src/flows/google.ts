import prompts from "prompts";
import type { Page } from "playwright";
import { launchSession, saveSession, closeSession } from "../lib/browser.js";
import { writeVwItem } from "../lib/vw.js";

export interface GoogleResult {
  projectId: string;
  clientId: string;
  clientSecret: string;
}

interface FlowOptions {
  installerName: string;
  installerEmail: string;
  freshLogin: boolean;
  dryRun: boolean;
}

const GCP_CONSOLE = "https://console.cloud.google.com";
const APIS_TO_ENABLE = [
  { name: "Gmail API", id: "gmail.googleapis.com" },
  { name: "Google Calendar API", id: "calendar-json.googleapis.com" },
  { name: "Google Drive API", id: "drive.googleapis.com" },
];

export async function runGoogleFlow(opts: FlowOptions): Promise<GoogleResult> {
  console.log("\n--- Google Cloud Console flow ---");

  if (opts.dryRun) {
    const projectName = `Behalf.bot - ${opts.installerName}`;
    console.log(`[dry-run] would open GCP console and create project: "${projectName}"`);
    console.log(`[dry-run] would enable APIs: ${APIS_TO_ENABLE.map((a) => a.name).join(", ")}`);
    console.log("[dry-run] would configure OAuth consent screen");
    console.log("[dry-run] would create Desktop OAuth client");
    console.log("[dry-run] would write VW item \"Behalf.bot - Google OAuth client\"");
    return {
      projectId: "behalf-bot-installer-dry-run",
      clientId: "000000000000-dryrun.apps.googleusercontent.com",
      clientSecret: "GOCSPX-dryrun_secret",
    };
  }

  const session = await launchSession("google", opts.freshLogin, opts.dryRun);
  if (!session) throw new Error("session should exist in wet mode");

  const { page } = session;
  let result: GoogleResult = { projectId: "", clientId: "", clientSecret: "" };

  try {
    await page.goto(GCP_CONSOLE);

    const loggedIn = await waitForGcpLogin(page);
    if (!loggedIn) {
      throw new Error("GCP login timed out (5 min). Restart the wizard and try again.");
    }

    const projectId = await createGcpProject(page, opts.installerName);
    result.projectId = projectId;
    console.log(`  Created GCP project: ${projectId}`);

    await enableApis(page, projectId);

    await configureOauthConsentScreen(page, projectId, opts);

    const { clientId, clientSecret } = await createOauthClient(page, projectId, opts);
    result.clientId = clientId;
    result.clientSecret = clientSecret;

    writeVwItem(
      {
        name: "Behalf.bot - Google OAuth client",
        username: clientId,
        password: clientSecret,
      },
      opts.dryRun
    );

    console.log('  Wrote VW item "Behalf.bot - Google OAuth client".');

    await saveSession(session, "google", opts.dryRun);
  } finally {
    await closeSession(session);
  }

  return result;
}

async function waitForGcpLogin(page: Page): Promise<boolean> {
  const fiveMinutes = 5 * 60 * 1000;
  const start = Date.now();

  while (Date.now() - start < fiveMinutes) {
    const url = page.url();
    if (url.includes("console.cloud.google.com") && !url.includes("accounts.google.com")) {
      return true;
    }
    // Also accept if we see the project selector UI
    const hasDashboard = await page
      .locator('[data-test-id="project-selector"]')
      .isVisible()
      .catch(() => false);
    if (hasDashboard) return true;

    console.log("  Waiting for Google Cloud Console login in browser window...");
    await page.waitForTimeout(5_000);
  }
  return false;
}

async function createGcpProject(page: Page, installerName: string): Promise<string> {
  const projectName = `Behalf.bot - ${installerName}`;

  await page.goto(`${GCP_CONSOLE}/projectcreate`);
  await page.waitForLoadState("networkidle");

  // Fill project name
  const nameInput = page.locator('input[id="p-name"]').first();
  await nameInput.waitFor({ timeout: 15_000 });
  await nameInput.fill(projectName);

  // The project ID is auto-generated; capture it after a short delay
  await page.waitForTimeout(2_000);
  const idInput = page.locator('input[id="p-id"]').first();
  const projectId = await idInput.inputValue();

  await page.getByRole("button", { name: /create/i }).click();

  // Wait for project creation to complete (redirect + dashboard load)
  await page.waitForURL(/console\.cloud\.google\.com/, { timeout: 60_000 });
  await page.waitForLoadState("networkidle");

  return projectId;
}

async function enableApis(page: Page, projectId: string): Promise<void> {
  console.log("  Enabling APIs...");
  for (const api of APIS_TO_ENABLE) {
    const enableUrl =
      `${GCP_CONSOLE}/apis/library/${api.id}?project=${projectId}`;
    await page.goto(enableUrl);
    await page.waitForLoadState("networkidle");

    const enableBtn = page.getByRole("button", { name: /^enable$/i }).first();
    const isAlreadyEnabled = await page
      .getByText(/api enabled|api is enabled/i)
      .isVisible()
      .catch(() => false);

    if (!isAlreadyEnabled) {
      await enableBtn.click();
      await page.waitForLoadState("networkidle");
    }

    console.log(`  Enabled: ${api.name}`);
  }
}

async function configureOauthConsentScreen(
  page: Page,
  projectId: string,
  opts: FlowOptions
): Promise<void> {
  console.log("  Configuring OAuth consent screen...");

  const consentUrl =
    `${GCP_CONSOLE}/apis/credentials/consent?project=${projectId}`;
  await page.goto(consentUrl);
  await page.waitForLoadState("networkidle");

  // Select "External" user type (unless the account is a Google Workspace org)
  const externalRadio = page.getByLabel(/external/i).first();
  if (await externalRadio.isVisible()) {
    await externalRadio.check();
    await page.getByRole("button", { name: /create/i }).click();
    await page.waitForLoadState("networkidle");
  }

  // Step 1: App info
  const appNameInput = page
    .locator('input[id="mat-input-0"], input[placeholder*="app name" i]')
    .first();
  await appNameInput.waitFor({ timeout: 10_000 });
  await appNameInput.fill(`Behalf.bot - ${opts.installerName}`);

  // Support email
  const supportEmailSelect = page
    .locator('mat-select[formcontrolname="userSupportEmail"]')
    .first();
  if (await supportEmailSelect.isVisible()) {
    await supportEmailSelect.click();
    await page.getByText(opts.installerEmail, { exact: false }).first().click();
  }

  // Developer contact email (same address, at bottom of page)
  const devContactInput = page
    .locator('input[placeholder*="contact" i]')
    .last();
  if (await devContactInput.isVisible()) {
    await devContactInput.fill(opts.installerEmail);
  }

  await page.getByRole("button", { name: /save and continue/i }).click();
  await page.waitForLoadState("networkidle");

  // Step 2: Scopes - skip (scopes come from the OAuth client, not consent screen)
  const saveContinueBtn = page
    .getByRole("button", { name: /save and continue/i })
    .first();
  if (await saveContinueBtn.isVisible()) {
    await saveContinueBtn.click();
    await page.waitForLoadState("networkidle");
  }

  // Step 3: Test users - add installer email as test user
  const addUsersBtn = page
    .getByRole("button", { name: /add users/i })
    .first();
  if (await addUsersBtn.isVisible()) {
    await addUsersBtn.click();
    const emailInput = page
      .locator('textarea[placeholder*="email" i], input[placeholder*="email" i]')
      .last();
    await emailInput.fill(opts.installerEmail);
    await page.getByRole("button", { name: /add/i }).last().click();
  }

  const finalSaveBtn = page
    .getByRole("button", { name: /save and continue/i })
    .first();
  if (await finalSaveBtn.isVisible()) {
    await finalSaveBtn.click();
    await page.waitForLoadState("networkidle");
  }

  console.log("  OAuth consent screen configured.");
}

async function createOauthClient(
  page: Page,
  projectId: string,
  opts: FlowOptions
): Promise<{ clientId: string; clientSecret: string }> {
  console.log("  Creating Desktop OAuth client...");

  const credentialsUrl =
    `${GCP_CONSOLE}/apis/credentials?project=${projectId}`;
  await page.goto(credentialsUrl);
  await page.waitForLoadState("networkidle");

  // Click "+ Create Credentials" -> "OAuth client ID"
  await page.getByRole("button", { name: /create credentials/i }).click();
  await page.getByText(/oauth client id/i).first().click();
  await page.waitForLoadState("networkidle");

  // Application type = Desktop app
  const appTypeSelect = page
    .locator('mat-select[formcontrolname="applicationType"]')
    .first();
  await appTypeSelect.waitFor({ timeout: 10_000 });
  await appTypeSelect.click();
  await page.getByText(/desktop app/i).first().click();

  // Client name
  const clientNameInput = page
    .locator('input[formcontrolname="displayName"]')
    .first();
  await clientNameInput.fill(`Behalf.bot - ${opts.installerName}`);

  await page.getByRole("button", { name: /create/i }).click();
  await page.waitForLoadState("networkidle");

  // The dialog shows Client ID + Client Secret
  const clientIdLocator = page
    .locator('[data-test-id="client-id"], .client-id-value')
    .first();
  const clientSecretLocator = page
    .locator('[data-test-id="client-secret"], .client-secret-value')
    .first();

  let clientId = await clientIdLocator.textContent().catch(() => "");
  let clientSecret = await clientSecretLocator.textContent().catch(() => "");

  clientId = clientId?.trim() ?? "";
  clientSecret = clientSecret?.trim() ?? "";

  if (!clientId || !clientSecret) {
    console.warn(
      "  Could not auto-capture OAuth client credentials from the dialog."
    );
    console.warn("  Please copy the Client ID and Client Secret from the browser.");

    const { manualClientId } = await prompts({
      type: "text",
      name: "manualClientId",
      message: "Paste the Client ID:",
    });
    const { manualClientSecret } = await prompts({
      type: "password",
      name: "manualClientSecret",
      message: "Paste the Client Secret:",
    });

    clientId = manualClientId as string;
    clientSecret = manualClientSecret as string;
  }

  // Dismiss the dialog
  await page.getByRole("button", { name: /ok|close/i }).first().click().catch(() => null);

  return { clientId, clientSecret };
}
