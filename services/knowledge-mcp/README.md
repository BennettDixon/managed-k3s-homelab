# knowledge-mcp

> **Alpha, and mostly AI-written.** This service was spec'd, implemented, and
> adversarially reviewed by Claude agent sessions under human direction, and
> runs here as a personal-infrastructure testbed while its operator kicks the
> tires. Expect sharp edges; interfaces and internals may change without
> notice. A polished, properly packaged open-source version will likely be a
> later rewrite — don't build on this one.

`knowledge-mcp` is the retrieval MCP service of the personal cloud. It answers
`search` and `fetch` over registered corpora and keeps them current via
`ingest`/`reingest`, backed by one SQLite+FTS5 file on a PVC. Corpus #1 is
this repo's own operational notes (docs/ + proxmox/). Depends on:
tailscale-operator (exposure), external-secrets (caller tokens), Harbor
(image), and jobs-mcp (scheduled re-ingest rides a `knowledge-reingest`
task). Vector search, NanoClaw access, and non-git corpora are seams, not
features. Spec: `docs/specs/knowledge-mcp.md` (approved 2026-09-02).

Honest v1 note (spec stance): for corpus #1 and a client that already holds a
repo checkout, BM25-over-MCP adds little over grep — v1's product is the
governance/auth/provenance/eval skeleton and service to checkout-less
clients; corpus #2 is the adoption test. Known lexical blind spot:
vocabulary-gap paraphrases miss; the golden eval keeps that measured
(`expect: miss` entries) rather than hidden.

## Slice 1 (this directory)

Code + tests only — no manifests, no infra. `npm test` runs the suite;
`npm run eval` runs the golden-query retrieval eval against a scratch index
built from the working tree's docs/ + proxmox/.
