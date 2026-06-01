import { execSync, spawnSync } from "node:child_process";

export interface VwItem {
  name: string;
  password?: string;
  username?: string;
  notes?: string;
}

export interface VwConfig {
  email: string;
  url: string;
  masterPassword: string;
  dryRun: boolean;
}

export function assertRbwInstalled(): void {
  const result = spawnSync("which", ["rbw"]);
  if (result.status !== 0) {
    console.error("\n  rbw is not installed on this machine.");
    console.error("  Install it with:");
    console.error("    cargo install rbw");
    console.error("  or via your package manager (Homebrew: brew install rbw).");
    console.error("  https://github.com/doy/rbw\n");
    process.exit(1);
  }
}

export function configureRbw(config: VwConfig): void {
  if (config.dryRun) {
    console.log(
      `[dry-run] rbw config set email ${config.email}`
    );
    console.log(
      `[dry-run] rbw config set base_url ${config.url}`
    );
    return;
  }
  execSync(`rbw config set email ${JSON.stringify(config.email)}`, {
    stdio: "pipe",
  });
  execSync(`rbw config set base_url ${JSON.stringify(config.url)}`, {
    stdio: "pipe",
  });
}

export function unlockRbw(config: VwConfig): void {
  if (config.dryRun) {
    console.log("[dry-run] rbw unlock (skipped)");
    return;
  }
  const result = spawnSync("rbw", ["unlock"], {
    input: config.masterPassword + "\n",
    stdio: ["pipe", "pipe", "pipe"],
    encoding: "utf8",
  });
  if (result.status !== 0) {
    console.error(
      "  rbw unlock failed. Check that your master password is correct and VW is reachable."
    );
    console.error(result.stderr);
    process.exit(1);
  }
}

export function lockRbw(dryRun: boolean): void {
  if (dryRun) {
    console.log("[dry-run] rbw lock (skipped)");
    return;
  }
  spawnSync("rbw", ["lock"], { stdio: "pipe" });
}

function rbwItemExists(itemName: string): boolean {
  const result = spawnSync("rbw", ["get", itemName], {
    stdio: "pipe",
    encoding: "utf8",
  });
  return result.status === 0;
}

export function writeVwItem(item: VwItem, dryRun: boolean): void {
  if (dryRun) {
    console.log(`[dry-run] VW write: "${item.name}"`);
    if (item.username !== undefined)
      console.log(`  username = ${item.username}`);
    if (item.password !== undefined)
      console.log(`  password = ${"*".repeat(Math.min(8, item.password.length))}...`);
    if (item.notes !== undefined)
      console.log(`  notes (${item.notes.split("\n").length} lines)`);
    return;
  }

  const exists = rbwItemExists(item.name);

  if (exists) {
    if (item.password !== undefined) {
      const result = spawnSync(
        "rbw",
        ["edit", item.name],
        {
          input: item.password,
          stdio: ["pipe", "pipe", "pipe"],
          encoding: "utf8",
        }
      );
      if (result.status !== 0) {
        throw new Error(`rbw edit failed for "${item.name}": ${result.stderr}`);
      }
    }
    if (item.notes !== undefined) {
      const result = spawnSync(
        "rbw",
        ["edit", "--field", "notes", item.name],
        {
          input: item.notes,
          stdio: ["pipe", "pipe", "pipe"],
          encoding: "utf8",
        }
      );
      if (result.status !== 0) {
        throw new Error(
          `rbw edit notes failed for "${item.name}": ${result.stderr}`
        );
      }
    }
  } else {
    // rbw add reads: name\nusername\npassword\n from stdin in interactive mode,
    // but the non-interactive path uses --no-interact + URI args.
    // rbw add <name> [username] writes via pipe.
    const args = ["add"];
    if (item.username !== undefined) {
      args.push("--no-interact", item.name, item.username);
    } else {
      args.push("--no-interact", item.name);
    }

    const passwordInput = item.password ?? "";
    const result = spawnSync("rbw", args, {
      input: passwordInput + "\n",
      stdio: ["pipe", "pipe", "pipe"],
      encoding: "utf8",
    });
    if (result.status !== 0) {
      throw new Error(`rbw add failed for "${item.name}": ${result.stderr}`);
    }

    if (item.notes !== undefined) {
      const notesResult = spawnSync(
        "rbw",
        ["edit", "--field", "notes", item.name],
        {
          input: item.notes,
          stdio: ["pipe", "pipe", "pipe"],
          encoding: "utf8",
        }
      );
      if (notesResult.status !== 0) {
        throw new Error(
          `rbw edit notes failed for "${item.name}": ${notesResult.stderr}`
        );
      }
    }
  }
}
