import prompts from "prompts";
import { assertRbwInstalled, configureRbw, unlockRbw, lockRbw } from "./lib/vw.js";
import { runNotionFlow } from "./flows/notion.js";
import { runGoogleFlow } from "./flows/google.js";
import { storageStatePath } from "./lib/browser.js";

const DRY_RUN = process.argv.includes("--dry-run");
const FRESH_LOGIN = process.argv.includes("--fresh-login");

interface InstallerInfo {
  name: string;
  email: string;
  vwUrl: string;
  vwEmail: string;
  enableNotion: boolean;
  enableGoogle: boolean;
}

async function main(): Promise<void> {
  console.log("\n=================================================");
  console.log("  Behalf.bot Setup Wizard - Phase 1");
  console.log("  Notion + Google credential provisioning");
  if (DRY_RUN) console.log("  MODE: DRY RUN - no browser, no VW writes");
  if (FRESH_LOGIN) console.log("  FLAG: --fresh-login - will sign in fresh to each service");
  console.log("=================================================\n");

  console.log(
    "This wizard automates the credential setup steps described in"
  );
  console.log(
    "docs/installer-vw-template.md. It will:"
  );
  console.log("  1. Open a visible browser (you sign in, wizard does the rest)");
  console.log("  2. Create the required integrations and OAuth clients");
  console.log("  3. Write the resulting tokens directly to your Vaultwarden\n");

  // Verify rbw is available before doing anything else
  assertRbwInstalled();

  const info = await gatherInstallerInfo();

  if (!DRY_RUN) {
    // Configure rbw before prompting for master password
    configureRbw({
      email: info.vwEmail,
      url: info.vwUrl,
      masterPassword: "",
      dryRun: DRY_RUN,
    });

    const { vwMasterPassword } = await prompts({
      type: "password",
      name: "vwMasterPassword",
      message: "Vaultwarden master password (stored only in memory, not on disk):",
    });

    unlockRbw({
      email: info.vwEmail,
      url: info.vwUrl,
      masterPassword: vwMasterPassword as string,
      dryRun: DRY_RUN,
    });

    console.log("  Vaultwarden unlocked.\n");
  } else {
    console.log("[dry-run] VW unlock skipped.\n");
  }

  const writtenItems: string[] = [];

  try {
    if (info.enableNotion) {
      const notionResult = await runNotionFlow({
        installerName: info.name,
        freshLogin: FRESH_LOGIN,
        dryRun: DRY_RUN,
      });
      writtenItems.push("Behalf.bot - Notion integration token");
      if (Object.keys(notionResult.databases).length > 0) {
        writtenItems.push("Behalf.bot - Notion DB IDs");
      }
    }

    if (info.enableGoogle) {
      await runGoogleFlow({
        installerName: info.name,
        installerEmail: info.email,
        freshLogin: FRESH_LOGIN,
        dryRun: DRY_RUN,
      });
      writtenItems.push("Behalf.bot - Google OAuth client");
    }
  } finally {
    lockRbw(DRY_RUN);
    console.log("\n  Vaultwarden locked.");
  }

  printSummary(info, writtenItems);
}

async function gatherInstallerInfo(): Promise<InstallerInfo> {
  const responses = await prompts([
    {
      type: "text",
      name: "name",
      message: "Your full name (used as the integration owner name):",
      validate: (v: string) => v.length > 0 || "Name is required",
    },
    {
      type: "text",
      name: "email",
      message: "Your Google / work email:",
      validate: (v: string) => v.includes("@") || "Valid email required",
    },
    {
      type: "text",
      name: "vwUrl",
      message: "Vaultwarden URL (e.g. https://vault.yourdomain.ts.net):",
      initial: "https://vault.yourdomain.ts.net",
      validate: (v: string) => v.startsWith("http") || "Must be a full URL",
    },
    {
      type: "text",
      name: "vwEmail",
      message: "Vaultwarden account email:",
      validate: (v: string) => v.includes("@") || "Valid email required",
    },
    {
      type: "confirm",
      name: "enableNotion",
      message: "Set up Notion integration? (skip if not using Notion plugin)",
      initial: true,
    },
    {
      type: "confirm",
      name: "enableGoogle",
      message: "Set up Google OAuth client? (Gmail, Calendar, Drive APIs)",
      initial: true,
    },
  ]);

  return responses as InstallerInfo;
}

function printSummary(info: InstallerInfo, writtenItems: string[]): void {
  console.log("\n=================================================");
  console.log("  Setup Complete");
  console.log("=================================================\n");

  if (writtenItems.length === 0) {
    console.log("  No items were written (all flows were skipped or dry-run).");
  } else {
    console.log("  Written to Vaultwarden:");
    for (const item of writtenItems) {
      console.log(`    - "${item}"`);
    }
  }

  console.log("\n  Session state saved at:");
  if (info.enableNotion) {
    console.log(`    Notion: ${storageStatePath("notion")}`);
  }
  if (info.enableGoogle) {
    console.log(`    Google: ${storageStatePath("google")}`);
  }

  console.log("\n  Phase 2 (FDC + Oura) and Phase 3 (Discord - manual) are not");
  console.log("  yet automated. See the README for instructions.");

  console.log("\n  You are ready to ping Sean + ${ASSISTANT_NAME} to kick off the SSH install phase.");
  console.log("  >>> All done. ✓\n");
}

main().catch((err) => {
  console.error("\nSetup wizard encountered an error:");
  console.error(err instanceof Error ? err.message : String(err));
  process.exit(1);
});
