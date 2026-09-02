import { describe, expect, it } from "vitest";
import { chunkMarkdown } from "../src/chunker.js";

// The measured hazard from the real corpus (proxmox/edgepve.md): column-0 "#"
// comment lines inside a bare fence must not split chunks.
const FENCED = `# Host: edgepve

## Parameters

Some parameter prose that is long enough to stand as its own chunk when the
minimum-size merge rule is applied to the surrounding content, padding padding
padding padding padding padding padding padding padding padding padding.

## /etc/network/interfaces (target)

\`\`\`
auto lo
# management — dedicated 2.5G port, untagged access port on the mgmt subnet
iface vmbr0 inet static
# guest trunk — 10G port, VLAN-aware, no host IP; switch side = trunk port
iface vmbr1 inet manual
\`\`\`

Trailing prose after the fence that also needs enough length to survive the
merge-forward rule so this section stays independent, padding padding padding
padding padding padding padding padding padding padding padding padding.
`;

describe("fence-aware chunking (spec §5)", () => {
  it("never splits on # lines inside fences", () => {
    const { chunks } = chunkMarkdown("t:edgepve.md", FENCED);
    const headings = chunks.map((c) => c.headingPath);
    expect(headings).toEqual(["Parameters", "/etc/network/interfaces (target)"]);
    const fenceChunk = chunks.find((c) => c.headingPath.includes("interfaces"));
    expect(fenceChunk!.body).toContain("# management — dedicated 2.5G port");
    expect(fenceChunk!.body).toContain("# guest trunk — 10G port");
  });

  it("extracts the H1 title", () => {
    expect(chunkMarkdown("t:d.md", FENCED).title).toBe("Host: edgepve");
  });

  it("gives duplicate headings stable ordinal suffixes within one doc", () => {
    const dup = `# T\n\n## Parameters\n\n${"x".repeat(300)}\n\n## Parameters\n\n${"y".repeat(300)}\n`;
    const { chunks } = chunkMarkdown("t:dup.md", dup);
    expect(chunks.map((c) => c.chunkId)).toEqual(["t:dup.md#parameters", "t:dup.md#parameters~1"]);
  });

  it("merges tiny heading-less fragments forward instead of emitting stubs", () => {
    const doc = `# T\n\nshort intro\n\n## Real Section\n\n${"content ".repeat(60)}\n`;
    const { chunks } = chunkMarkdown("t:m.md", doc);
    expect(chunks).toHaveLength(1);
    expect(chunks[0]!.body).toContain("short intro");
    expect(chunks[0]!.headingPath).toBe("Real Section");
  });

  it("short HEADED sections stand alone — provenance and heading boost intact", () => {
    const doc = `# T\n\n## Tiny But Real\n\ntwo lines only\nof runbook steps\n\n## Next Section\n\n${"content ".repeat(60)}\n`;
    const { chunks } = chunkMarkdown("t:s.md", doc);
    expect(chunks.map((c) => c.headingPath)).toEqual(["Tiny But Real", "Next Section"]);
    expect(chunks[0]!.body).toContain("two lines only");
  });

  it("splits oversized sections at paragraph bounds with the heading repeated", () => {
    const paras = Array.from({ length: 8 }, (_, i) => `${"p".repeat(900)} para${i}`).join("\n\n");
    const doc = `# T\n\n## Big\n\n${paras}\n`;
    const { chunks } = chunkMarkdown("t:big.md", doc);
    expect(chunks.length).toBeGreaterThan(1);
    for (const c of chunks) {
      expect(c.headingPath).toBe("Big");
      expect(c.body.length).toBeLessThanOrEqual(5_000);
    }
    expect(chunks[1]!.body.startsWith("## Big")).toBe(true);
  });

  it("nests h3 under the governing h2 in heading_path", () => {
    const doc = `# T\n\n## Outer\n\n${"o".repeat(250)}\n\n### Inner\n\n${"i".repeat(250)}\n`;
    const { chunks } = chunkMarkdown("t:h.md", doc);
    expect(chunks.map((c) => c.headingPath)).toEqual(["Outer", "Outer > Inner"]);
  });

  it("~~~ fences behave like backtick fences", () => {
    const doc = `# T\n\n## S\n\n~~~\n## not a heading\n~~~\n\n${"z".repeat(250)}\n`;
    const { chunks } = chunkMarkdown("t:t.md", doc);
    expect(chunks).toHaveLength(1);
    expect(chunks[0]!.body).toContain("## not a heading");
  });
});
