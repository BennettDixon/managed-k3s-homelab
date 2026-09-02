import { defineConfig } from "vitest/config";

// Unit tests only; the golden eval (eval/) has its own config + npm script —
// it reads the repo working tree, which unit tests must never depend on.
export default defineConfig({
  test: {
    include: ["test/**/*.test.ts"],
  },
});
