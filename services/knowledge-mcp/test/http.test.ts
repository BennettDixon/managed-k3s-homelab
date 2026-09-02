import { describe, expect, it } from "vitest";
import type { AddressInfo } from "node:net";
import { buildApp } from "../src/http.js";
import { testStore, testRegistry, testConfig, testMetrics, seedDoc, RAW_PREFIX } from "./helpers.js";

function appFixture(opts: { registryError?: string | null } = {}) {
  const { db, store } = testStore();
  const registry = testRegistry();
  const config = testConfig();
  const metrics = testMetrics(store, registry);
  seedDoc(
    store,
    "homelab-notes:docs/runbooks/jobs-mcp.md",
    "homelab-notes",
    `${RAW_PREFIX}docs/runbooks/jobs-mcp.md`,
    `# Runbook: jobs-mcp\n\n## Bridge down checklist\n\nIs the n8n LXC up? Check the egress Service and cluster-vars secret values.\nAlso verify the operator proxy still resolves and the ACL grant is intact.\n`,
  );
  seedDoc(
    store,
    "public-faq:faq/nas.md",
    "public-faq",
    `${RAW_PREFIX}faq/nas.md`,
    `# FAQ\n\n## What is the NAS called\n\nThe bulk NAS answers by its tailnet name and stores artifact uploads there.\nUse the artifacts convention documented in the runbook for job outputs.\n`,
    "curated",
  );
  const app = buildApp(db, store, registry, config, metrics, { registryError: opts.registryError ?? null });
  return { app, store };
}

async function serve(app: ReturnType<typeof appFixture>["app"]): Promise<{ base: string; close: () => Promise<void> }> {
  const server = app.listen(0);
  await new Promise((r) => server.once("listening", r));
  const { port } = server.address() as AddressInfo;
  return {
    base: `http://127.0.0.1:${port}`,
    close: () => new Promise((r) => server.close(() => r(undefined))),
  };
}

async function mcpCall(base: string, token: string, name: string, args: Record<string, unknown>): Promise<{ http: number; body: any }> {
  const res = await fetch(`${base}/mcp`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      accept: "application/json, text/event-stream",
      authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/call", params: { name, arguments: args } }),
  });
  const text = await res.text();
  const data = text.split("\n").filter((l) => l.startsWith("data: ")).pop();
  const rpc = data ? JSON.parse(data.slice(6)) : JSON.parse(text || "{}");
  const inner = rpc?.result?.content?.[0]?.text;
  return { http: res.status, body: inner ? JSON.parse(inner) : rpc };
}

const OPERATOR = "operator-token-0123456789";
const NANOCLAW = "nanoclaw-token-0123456789";
const REINGEST = "reingest-token-0123456789";

describe("probes (spec §8)", () => {
  it("healthz and readyz are green on a healthy fixture", async () => {
    const { app } = appFixture();
    const { base, close } = await serve(app);
    try {
      expect((await fetch(`${base}/healthz`)).status).toBe(200);
      expect((await fetch(`${base}/readyz`)).status).toBe(200);
    } finally {
      await close();
    }
  });

  it("readyz is 503 while healthz stays 200 on a registry parse failure", async () => {
    const { app } = appFixture({ registryError: "boom" });
    const { base, close } = await serve(app);
    try {
      expect((await fetch(`${base}/healthz`)).status).toBe(200);
      const ready = await fetch(`${base}/readyz`);
      expect(ready.status).toBe(503);
      expect(((await ready.json()) as { reason: string }).reason).toContain("boom");
    } finally {
      await close();
    }
  });
});

describe("mcp transport auth (spec §2)", () => {
  it("401s missing/invalid bearers", async () => {
    const { app } = appFixture();
    const { base, close } = await serve(app);
    try {
      const res = await fetch(`${base}/mcp`, { method: "POST", headers: { "content-type": "application/json" }, body: "{}" });
      expect(res.status).toBe(401);
    } finally {
      await close();
    }
  });

  it("GET/DELETE /mcp are 405 with allow: POST", async () => {
    const { app } = appFixture();
    const { base, close } = await serve(app);
    try {
      const res = await fetch(`${base}/mcp`);
      expect(res.status).toBe(405);
      expect(res.headers.get("allow")).toBe("POST");
    } finally {
      await close();
    }
  });
});

describe("tool dispatch end-to-end (spec §3/§4)", () => {
  it("operator searches homelab-notes and gets provenance-labeled hits", async () => {
    const { app } = appFixture();
    const { base, close } = await serve(app);
    try {
      const r = await mcpCall(base, OPERATOR, "search", { corpus: "homelab-notes", query: "bridge down checklist" });
      expect(r.body.retrieval).toBe("bm25");
      expect(r.body.results[0].doc_id).toBe("homelab-notes:docs/runbooks/jobs-mcp.md");
      expect(r.body.results[0].trust).toBe("operator-authored");
    } finally {
      await close();
    }
  });

  it("frontend caller gets E_NOT_FOUND for an operator-only corpus — no existence oracle", async () => {
    const { app } = appFixture();
    const { base, close } = await serve(app);
    try {
      const r = await mcpCall(base, NANOCLAW, "search", { corpus: "homelab-notes", query: "anything" });
      expect(r.body.code).toBe("E_NOT_FOUND");
      const ok = await mcpCall(base, NANOCLAW, "search", { corpus: "public-faq", query: "NAS called" });
      expect(ok.body.results.length).toBeGreaterThan(0);
    } finally {
      await close();
    }
  });

  it("frontend caller is denied ingest at the capability gate (E_FORBIDDEN)", async () => {
    const { app } = appFixture();
    const { base, close } = await serve(app);
    try {
      const r = await mcpCall(base, NANOCLAW, "ingest", { corpus: "public-faq", uri: `${RAW_PREFIX}faq/nas.md` });
      expect(r.body.code).toBe("E_FORBIDDEN");
    } finally {
      await close();
    }
  });

  it("frontend fetch of an operator-corpus doc re-checks visibility server-side", async () => {
    const { app } = appFixture();
    const { base, close } = await serve(app);
    try {
      const denied = await mcpCall(base, NANOCLAW, "fetch", { doc_id: "homelab-notes:docs/runbooks/jobs-mcp.md" });
      expect(denied.body.code).toBe("E_NOT_FOUND");
      const allowed = await mcpCall(base, NANOCLAW, "fetch", { doc_id: "public-faq:faq/nas.md" });
      expect(allowed.body.content).toContain("tailnet name");
    } finally {
      await close();
    }
  });

  it("non-operator-authored content ships inside the untrusted envelope (spec §7)", async () => {
    const { app } = appFixture();
    const { base, close } = await serve(app);
    try {
      // public-faq is trust: curated — enveloped for EVERY caller class.
      const fetched = await mcpCall(base, OPERATOR, "fetch", { doc_id: "public-faq:faq/nas.md" });
      expect(fetched.body.content).toMatch(/^\[UNTRUSTED DOCUMENT CONTENT/);
      expect(fetched.body.content).toMatch(/\[END UNTRUSTED DOCUMENT CONTENT\]$/);
      const searched = await mcpCall(base, NANOCLAW, "search", { corpus: "public-faq", query: "NAS called" });
      expect(searched.body.results[0].text).toMatch(/^\[UNTRUSTED DOCUMENT CONTENT/);
      // operator-authored content is NOT wrapped.
      const clean = await mcpCall(base, OPERATOR, "search", { corpus: "homelab-notes", query: "bridge down checklist" });
      expect(clean.body.results[0].text).not.toContain("UNTRUSTED");
    } finally {
      await close();
    }
  });

  it("tools/list is filtered per caller class", async () => {
    const { app } = appFixture();
    const { base, close } = await serve(app);
    try {
      const res = await fetch(`${base}/mcp`, {
        method: "POST",
        headers: { "content-type": "application/json", accept: "application/json, text/event-stream", authorization: `Bearer ${REINGEST}` },
        body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/list" }),
      });
      const text = await res.text();
      const data = text.split("\n").filter((l) => l.startsWith("data: ")).pop()!;
      const names = JSON.parse(data.slice(6)).result.tools.map((t: { name: string }) => t.name);
      expect(names.sort()).toEqual(["list_corpora", "reingest"]);
    } finally {
      await close();
    }
  });

  it("ingest with a content parameter is rejected E_UNSUPPORTED (push seam reserved)", async () => {
    const { app } = appFixture();
    const { base, close } = await serve(app);
    try {
      const r = await mcpCall(base, OPERATOR, "ingest", { corpus: "homelab-notes", uri: `${RAW_PREFIX}docs/x.md`, content: "# X" });
      expect(r.body.code).toBe("E_UNSUPPORTED");
    } finally {
      await close();
    }
  });

  it("list_corpora shows only visible corpora with freshness", async () => {
    const { app } = appFixture();
    const { base, close } = await serve(app);
    try {
      const r = await mcpCall(base, NANOCLAW, "list_corpora", {});
      expect(r.body.corpora.map((c: { name: string }) => c.name)).toEqual(["public-faq"]);
      expect(r.body.corpora[0].docs).toBe(1);
    } finally {
      await close();
    }
  });
});
