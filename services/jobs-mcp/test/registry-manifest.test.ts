import { readFileSync, readdirSync } from "node:fs";
import { join, resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { parseRegistry } from "../src/registry.js";

// The DEPLOYED registry (apps/base/jobs-mcp/registry.yaml) under the service's
// own admission rules: a strict-Ajv compile failure or a bad entry fails
// readiness on the pod and leaves the Flux apps Kustomization NotReady
// (wait: true). Plus the spec §4 CI-checkable invariant: every task_type
// ships its canonical workflow export in n8n/, NAMED after the task_type
// (the startup check matches by name) with one POST Webhook node on exactly
// its webhook_path. Deliberately reads outside the service dir — that file
// is the one artifact these admission rules own.

const REPO_ROOT = resolve(import.meta.dirname, "../../..");
const REGISTRY = join(REPO_ROOT, "apps/base/jobs-mcp/registry.yaml");
const N8N_DIR = join(REPO_ROOT, "n8n");

interface WorkflowNode {
  type: string;
  parameters?: {
    path?: string;
    httpMethod?: string;
    url?: string;
    jsCode?: string;
    headerParameters?: { parameters?: { name: string; value: string }[] };
    options?: { timeout?: number };
  };
}
interface Workflow {
  name: string;
  nodes: (WorkflowNode & { name?: string })[];
  connections?: Record<string, { main: { node: string }[][] }>;
  settings?: Record<string, unknown>;
}

describe("deployed registry (apps/base/jobs-mcp/registry.yaml)", () => {
  const registry = parseRegistry(readFileSync(REGISTRY, "utf8"));
  const workflows = readdirSync(N8N_DIR)
    .filter((f) => f.endsWith(".json"))
    .map((f) => JSON.parse(readFileSync(join(N8N_DIR, f), "utf8")) as Workflow);

  it("parses under admission rules with every payload_schema compiled", () => {
    expect(registry.size).toBeGreaterThan(0);
    for (const [name, entry] of registry) {
      expect(entry.executor, name).toBe("n8n");
      expect(entry.idempotent, name).toBe(true);
      expect(entry.timeout_s, name).toBeLessThanOrEqual(300);
    }
  });

  it("every task_type ships its canonical n8n export: name == task_type, one POST webhook on webhook_path", () => {
    for (const [name, entry] of registry) {
      const wf = workflows.find((w) => w.name === name);
      expect(wf, `n8n/*.json with name ${name}`).toBeDefined();
      const hooks = wf!.nodes.filter((n) => n.type === "n8n-nodes-base.webhook");
      expect(hooks.map((h) => h.parameters?.path), name).toEqual([entry.webhook_path]);
      expect(hooks[0]?.parameters?.httpMethod, name).toBe("POST");
    }
  });

  it("knowledge-reingest: payload fence and timeout ordering hold", () => {
    const kr = registry.get("knowledge-reingest");
    expect(kr).toBeDefined();
    expect(kr!.frontend_allowed).toBe(false);
    const valid = kr!.validatePayload!;
    expect(valid({ corpus: "homelab-notes" })).toBe(true);
    expect(valid({ corpus: "Bad Corpus!" })).toBe(false);
    expect(valid({})).toBe(false);
    expect(valid({ corpus: "x", extra: 1 })).toBe(false);
    // The executor must give up before the dispatcher does, so a slow
    // knowledge-mcp fails as a visible n8n execution error, never an
    // orphaned execution (review, 2026-09-02).
    const wf = workflows.find((w) => w.name === "knowledge-reingest")!;
    const http = wf.nodes.find((n) => n.type === "n8n-nodes-base.httpRequest")!;
    expect(http.parameters?.options?.timeout).toBeLessThan(kr!.timeout_s * 1000);
  });

  it("gateway-smoke: payload fence, wiring, timeouts, and the executor's gateway fences", () => {
    const gs = registry.get("gateway-smoke");
    expect(gs).toBeDefined();
    expect(gs!.frontend_allowed).toBe(false);
    const valid = gs!.validatePayload!;
    expect(valid({})).toBe(true);
    expect(valid({ model: "haiku", prompt: "x".repeat(512) })).toBe(true);
    expect(valid({ prompt: "x".repeat(513) })).toBe(false); // 512 chars is always <= 2 KiB of UTF-8
    expect(valid({ model: "opus" })).toBe(false);
    expect(valid({ prompt: "" })).toBe(false);
    expect(valid({ extra: 1 })).toBe(false);

    const wf = workflows.find((w) => w.name === "gateway-smoke")!;
    const byName = (name: string) => wf.nodes.find((n) => n.name === name)!;
    // The ledger read sits on the only path to the call: Webhook -> Verify -> Authorized? -> Read job ledger -> Decide -> Needs call? -> Call gateway.
    const next = (from: string, branch = 0) => (wf.connections?.[from]?.main[branch] ?? []).map((c) => c.node);
    expect(next("Webhook")).toEqual(["Verify And Build"]);
    expect(next("Verify And Build")).toEqual(["Authorized?"]);
    expect(next("Authorized?", 0)).toEqual(["Read job ledger"]);
    expect(next("Authorized?", 1)).toEqual(["Respond"]);
    expect(next("Read job ledger")).toEqual(["Decide"]);
    expect(next("Decide")).toEqual(["Needs call?"]);
    expect(next("Needs call?", 0)).toEqual(["Call gateway"]);
    expect(next("Needs call?", 1)).toEqual(["Respond"]);
    expect(next("Call gateway")).toEqual(["Report"]);
    const callers = Object.entries(wf.connections ?? {}).filter(([, c]) => c.main.flat().some((x) => x.node === "Call gateway")).map(([k]) => k);
    expect(callers).toEqual(["Needs call?"]);

    const http = wf.nodes.filter((n) => n.type === "n8n-nodes-base.httpRequest");
    expect(http).toHaveLength(2);
    const ledger = byName("Read job ledger");
    const call = byName("Call gateway");
    // Exact URLs: only the gateway, only over the tailnet name.
    expect(ledger.parameters?.url).toBe("={{ 'http://gateway/ledger/jobs/' + encodeURIComponent($json.job_id) }}");
    expect(call.parameters?.url).toBe("http://gateway/v1/chat/completions");
    for (const n of http) {
      const headers = n.parameters?.headerParameters?.parameters ?? [];
      expect(headers.find((x) => x.name === "Authorization")?.value).toBe("=Bearer {{ $env.GATEWAY_EXECUTOR_TOKEN }}");
    }
    const h = Object.fromEntries((call.parameters?.headerParameters?.parameters ?? []).map((x) => [x.name, x.value]));
    expect(h["X-Gateway-Project"]).toBe("gateway-smoke");
    expect(h["X-Gateway-Job-Id"]).toBe("={{ $json.job_id }}");
    expect(h["X-Gateway-Budget-Cap-USD"]).toBe("={{ $json.cap_header }}");
    // Timeouts: the gateway's real bound for one request is count_tokens (10 s) + two
    // provider attempts of (header + 5 s margin) + one retry delay (<= 10 s); it must end
    // before n8n's call timeout, and the sequential HTTP nodes before the dispatcher's timeout_s.
    const gatewayBoundMs = (10 + 2 * (Number(h["X-Gateway-Timeout-S"]) + 5) + 10) * 1000;
    expect(gatewayBoundMs).toBeLessThan(call.parameters?.options?.timeout ?? 0);
    const total = http.reduce((sum, n) => sum + (n.parameters?.options?.timeout ?? Number.POSITIVE_INFINITY), 0);
    expect(total).toBeLessThan(gs!.timeout_s * 1000);

    // The first node after the webhook verifies the secret; the call is fixed at 64 tokens and a $0.10 cap fence.
    const verify = byName("Verify And Build").parameters?.jsCode ?? "";
    expect(verify).toContain("x-jobs-webhook-secret");
    expect(verify).toContain("max_tokens: 64");
    expect(verify).toContain("cap > 0.10");
    // A retry never claims a completion it did not see.
    expect(byName("Decide").parameters?.jsCode ?? "").toContain("prior_attempt_charged");
    // Successful executions are not saved: no prompt or completion in n8n history on the happy path.
    expect(wf.settings?.saveDataSuccessExecution).toBe("none");
    // Never a jobs-mcp bearer or a literal token in the export.
    expect(JSON.stringify(wf)).not.toMatch(/JOBS_MCP_BEARER_TOKEN|Bearer [0-9a-f]{16,}/);
  });
});
