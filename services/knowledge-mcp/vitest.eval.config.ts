import { defineConfig } from "vitest/config";

// `npm run eval` — the golden retrieval eval only (spec §5). Kept out of the
// default `npm test` run: it reads the repo working tree outside the service
// directory, which unit tests must never depend on.
export default defineConfig({
  test: {
    include: ["eval/**/*.test.ts"],
  },
});
