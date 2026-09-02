import { describe, expect, it } from "vitest";
import { parseRegistry, visibleCorpora, CLASS_TOOLS } from "../src/registry.js";
import { REGISTRY_YAML, testRegistry, caller } from "./helpers.js";

describe("registry admission (spec §4)", () => {
  it("parses the fixture", () => {
    const r = testRegistry();
    expect([...r.corpora.keys()]).toEqual(["homelab-notes", "public-faq"]);
    expect([...r.callers.keys()]).toEqual(["operator", "n8n-reingest", "nanoclaw"]);
  });

  it("rejects untrusted corpora with frontend visibility — quarantine is structural", () => {
    const bad = REGISTRY_YAML.replace("trust: curated", "trust: untrusted");
    expect(() => parseRegistry(bad)).toThrow(/untrusted.*frontend/);
  });

  it("rejects http (non-https) allow-list prefixes", () => {
    const bad = REGISTRY_YAML.replace("https://raw.githubusercontent.com/BennettDixon/managed-k3s-homelab/main/faq/", "http://example.com/faq/");
    expect(() => parseRegistry(bad)).toThrow(/https/);
  });

  it("rejects a caller pointing at an unknown corpus", () => {
    const bad = REGISTRY_YAML.replace("corpora: [homelab-notes]", "corpora: [nope]");
    expect(() => parseRegistry(bad)).toThrow(/unknown corpus/);
  });

  it("rejects a corpus without rebuild_source", () => {
    const bad = REGISTRY_YAML.replace("    rebuild_source: git\n    visibility: operator\n", "    visibility: operator\n");
    expect(() => parseRegistry(bad)).toThrow();
  });

  it("rejects unknown top-level and per-corpus keys (strict)", () => {
    expect(() => parseRegistry(REGISTRY_YAML + "\nextra: 1\n")).toThrow();
  });
});

describe("visibility (spec §4)", () => {
  it("frontend callers see only frontend corpora", () => {
    const r = testRegistry();
    const visible = visibleCorpora(r, caller(r, "nanoclaw"));
    expect([...visible.keys()]).toEqual(["public-faq"]);
  });

  it("operator sees everything", () => {
    const r = testRegistry();
    expect([...visibleCorpora(r, caller(r, "operator")).keys()]).toEqual(["homelab-notes", "public-faq"]);
  });

  it("a caller's corpora list narrows visibility further", () => {
    const r = testRegistry();
    expect([...visibleCorpora(r, caller(r, "n8n-reingest")).keys()]).toEqual(["homelab-notes"]);
  });
});

describe("capability table (spec §3)", () => {
  it("is deny-by-default per class", () => {
    expect(CLASS_TOOLS.frontend.has("ingest")).toBe(false);
    expect(CLASS_TOOLS.frontend.has("reingest")).toBe(false);
    expect(CLASS_TOOLS["reingest-bot"].has("search")).toBe(false);
    expect(CLASS_TOOLS.operator.has("ingest")).toBe(true);
  });
});
