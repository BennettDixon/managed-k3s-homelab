import { describe, expect, it } from "vitest";
import { buildMatch, normalizeScore } from "../src/query.js";
import { KnowledgeError } from "../src/errors.js";
import { testStore, seedDoc, testRegistry } from "./helpers.js";

describe("server-built MATCH (spec §5 — verified FTS5 hazards)", () => {
  it("quotes every term so hyphenated identifiers cannot error", () => {
    const q = buildMatch("jobs-mcp bridge");
    expect(q.and).toBe('"jobs-mcp" AND "bridge"');
    expect(q.or).toBe('"jobs-mcp" OR "bridge"');
  });

  it("neutralizes FTS5 operators and user quotes", () => {
    const q = buildMatch('NEAR NOT "spooky" input');
    expect(q.and).toContain('"NEAR"');
    expect(q.and).not.toContain('""');
  });

  it("rejects empty and oversized queries", () => {
    expect(() => buildMatch("   ")).toThrow(KnowledgeError);
    expect(() => buildMatch("x".repeat(600))).toThrow(/512/);
  });

  it("normalizes bm25 to positive descending", () => {
    expect(normalizeScore(-8.6e-7)).toBeGreaterThanOrEqual(0);
    expect(normalizeScore(-2)).toBeGreaterThan(normalizeScore(-1));
  });
});

describe("end-to-end retrieval against real FTS5", () => {
  const { store } = testStore();
  const registry = testRegistry();
  const corpus = registry.corpora.get("homelab-notes")!;
  seedDoc(
    store,
    "homelab-notes:docs/runbooks/jobs-mcp.md",
    "homelab-notes",
    "https://raw.githubusercontent.com/BennettDixon/managed-k3s-homelab/main/docs/runbooks/jobs-mcp.md",
    `# Runbook: jobs-mcp\n\n## Bridge down (jobs_bridge_up == 0) checklist\n\nIs the n8n LXC up? Does the egress Service exist with a real ExternalName?\nDoes cluster-vars exist in flux-system with the right FQDN value set?\n\n## Deploy / rollback\n\nImage is built and pushed from the workbench; deploys are image-tag bumps in\nthe deployment manifest and registry changes hash-roll the Deployment.\n`,
  );
  seedDoc(
    store,
    "homelab-notes:proxmox/edgepve.md",
    "homelab-notes",
    "https://raw.githubusercontent.com/BennettDixon/managed-k3s-homelab/main/proxmox/edgepve.md",
    `# Host: edgepve\n\n## Guest wiring\n\nThe gateway CT has a single leg on vmbr1 tagged into the servers VLAN via the\n10G trunk; the mgmt VLAN never reaches guests on this host by decision.\n`,
  );

  it("finds by underscore identifier (tokenized phrase match)", () => {
    const hits = store.search(corpus, "jobs_bridge_up", 6);
    expect(hits.length).toBeGreaterThan(0);
    expect(hits[0]!.doc_id).toBe("homelab-notes:docs/runbooks/jobs-mcp.md");
    expect(hits[0]!.heading_path).toContain("Bridge down");
  });

  it("heading hits outrank body hits (column weights)", () => {
    const hits = store.search(corpus, "bridge down checklist", 6);
    expect(hits[0]!.heading_path).toContain("Bridge down");
    expect(hits[0]!.score).toBeGreaterThan(0);
  });

  it("falls back to OR when AND over-constrains", () => {
    const hits = store.search(corpus, "vmbr1 nonexistentterm", 6);
    expect(hits.length).toBeGreaterThan(0);
    expect(hits[0]!.doc_id).toBe("homelab-notes:proxmox/edgepve.md");
  });

  it("scopes to the corpus and excludes tombstoned docs", () => {
    store.tombstone("homelab-notes:proxmox/edgepve.md", 2000);
    const hits = store.search(corpus, "servers VLAN trunk", 6);
    expect(hits.find((h) => h.doc_id.includes("edgepve"))).toBeUndefined();
  });
});
