import { readFileSync, readdirSync } from "node:fs";
import { join, resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { parseRegistry, visibleCorpora, CLASS_TOOLS } from "../src/registry.js";

// The DEPLOYED registry (apps/base/knowledge-mcp/corpora.yaml) parsed under
// the service's own admission rules (spec §4). A malformed registry fails
// readiness on the pod, which freezes the Flux apps chain (wait: true) — so
// this is the guard, and it runs on every PR touching that file. Lives with
// the eval (not the unit suite) because it reads outside the service dir.

const REPO_ROOT = resolve(import.meta.dirname, "../../..");
const REGISTRY_PATH = join(REPO_ROOT, "apps/base/knowledge-mcp/corpora.yaml");

describe("deployed registry (apps/base/knowledge-mcp/corpora.yaml)", () => {
  const registry = parseRegistry(readFileSync(REGISTRY_PATH, "utf8"));

  it("declares corpus #1 with the sign-off 3 posture: operator-only, operator-authored, git-rebuildable", () => {
    const notes = registry.corpora.get("homelab-notes");
    expect(notes).toBeDefined();
    expect(notes!.visibility).toBe("operator");
    expect(notes!.trust).toBe("operator-authored");
    expect(notes!.rebuild_source).toBe("git");
    expect(notes!.tree_source).toMatch(/^https:\/\/api\.github\.com\//);
  });

  it("declares exactly the two day-one callers with their classes", () => {
    expect([...registry.callers.keys()].sort()).toEqual(["n8n-reingest", "operator"]);
    expect(registry.callers.get("operator")!.class).toBe("operator");
    expect(registry.callers.get("n8n-reingest")!.class).toBe("reingest-bot");
  });

  it("scopes the reingest executor to homelab-notes and denies it search", () => {
    const bot = registry.callers.get("n8n-reingest")!;
    expect([...visibleCorpora(registry, bot).keys()]).toEqual(["homelab-notes"]);
    expect(CLASS_TOOLS[bot.class].has("reingest")).toBe(true);
    expect(CLASS_TOOLS[bot.class].has("search")).toBe(false);
  });

  it("stays public-repo safe: no tailnet FQDNs, private, or tailnet (CGNAT/ULA) addresses in the shipped files", () => {
    // Four-octet forms only (no false positives on version strings); the
    // 100.64/10 CGNAT range is every tailnet IPv4, fd7a:115c:a1e0 the ULA.
    const privateOrTailnet =
      /\b(10(\.\d{1,3}){3}|192\.168(\.\d{1,3}){2}|172\.(1[6-9]|2\d|3[01])(\.\d{1,3}){2}|100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])(\.\d{1,3}){2})\b|fd7a:115c:a1e0/i;
    const dir = join(REPO_ROOT, "apps/base/knowledge-mcp");
    const files = readdirSync(dir).map((f) => join(dir, f)).concat(join(REPO_ROOT, "docs/runbooks/knowledge-mcp.md"));
    for (const f of files) {
      const text = readFileSync(f, "utf8");
      expect(text, f).not.toMatch(/\.ts\.net/);
      expect(text, f).not.toMatch(privateOrTailnet);
    }
  });
});
