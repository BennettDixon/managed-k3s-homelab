import { timingSafeEqual } from "node:crypto";
import type { Config } from "./config.js";
import type { Caller, Registry } from "./registry.js";

// Auth middleware (spec §2): bearer token -> {caller_id, class} via the
// caller-token map. The check runs constant-time per candidate AND iterates
// every candidate regardless of an early match, so response timing never
// reveals which caller id (if any) a probed token belongs to.
export function resolveCaller(header: string | undefined, config: Config, registry: Registry): Caller | null {
  if (!header || !header.startsWith("Bearer ")) return null;
  const presented = Buffer.from(header.slice(7));
  let matchedId: string | null = null;
  for (const [id, token] of config.callerTokens) {
    const expected = Buffer.from(token);
    if (presented.length === expected.length && timingSafeEqual(presented, expected)) {
      matchedId = id;
    }
  }
  if (matchedId === null) return null;
  // A token whose caller id has no registry policy entry authenticates
  // NOBODY: the secret map and the repo registry must agree (fail closed —
  // a secret-only caller would otherwise bypass the PR-reviewed policy).
  return registry.callers.get(matchedId) ?? null;
}
