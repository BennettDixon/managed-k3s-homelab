import { describe, expect, it } from "vitest";
import { resolveCaller } from "../src/auth.js";
import { parseCallerTokens } from "../src/config.js";
import { testConfig, testRegistry } from "./helpers.js";

describe("caller-token map (spec §2)", () => {
  const registry = testRegistry();
  const config = testConfig();

  it("resolves a valid token to its caller with class", () => {
    const c = resolveCaller("Bearer operator-token-0123456789", config, registry);
    expect(c?.id).toBe("operator");
    expect(c?.class).toBe("operator");
  });

  it("rejects missing/malformed/unknown tokens", () => {
    expect(resolveCaller(undefined, config, registry)).toBeNull();
    expect(resolveCaller("operator-token-0123456789", config, registry)).toBeNull();
    expect(resolveCaller("Bearer nope", config, registry)).toBeNull();
  });

  it("a token whose caller id has no registry policy entry authenticates NOBODY (fail closed)", () => {
    const config2 = testConfig({
      callerTokens: parseCallerTokens(JSON.stringify({ ghost: "ghost-token-0123456789" })),
    });
    expect(resolveCaller("Bearer ghost-token-0123456789", config2, registry)).toBeNull();
  });
});

describe("token map parsing (spec §2 — loud boot failures)", () => {
  it("rejects non-JSON, arrays, short tokens, bad ids, empty maps", () => {
    expect(() => parseCallerTokens("not json")).toThrow(/valid JSON/);
    expect(() => parseCallerTokens('["a"]')).toThrow(/object/);
    expect(() => parseCallerTokens('{"operator": "short"}')).toThrow(/16 chars/);
    expect(() => parseCallerTokens('{"Bad Id!": "0123456789abcdef"}')).toThrow(/identifier/);
    expect(() => parseCallerTokens("{}")).toThrow(/no callers/);
  });
});
