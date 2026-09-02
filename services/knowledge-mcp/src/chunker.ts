// Fence-aware markdown chunker (spec §5). One heading section per chunk,
// split at ##/### — but NEVER on a "#" line inside a fenced block: the real
// corpus contains config-file comments at column 0 inside bare fences
// (proxmox/edgepve.md), and fence-blind splitting produced 12 false chunks
// out of 74 when measured. ~15 lines of fence state beat a markdown library
// dependency.

// Bump on ANY behavior change to this module. Ingest stamps it into meta;
// boot re-chunks every live doc on mismatch (store.rechunkAll) — otherwise
// search would serve ingest-time FTS rows while chunk ids/neighbors/fetch
// re-derive with the NEW chunker: silent desync, and the sha short-circuit
// means it never heals on its own (review finding, 2026-09-02).
export const CHUNKER_VERSION = 1;

export interface Chunk {
  chunkId: string;
  chunkIndex: number;
  headingPath: string;
  body: string;
}

const MAX_CHUNK_CHARS = 4_800; // ~1,200 tokens: split at paragraph bounds above this
const MIN_CHUNK_CHARS = 200; // below this, merge forward into the next section

function slugify(heading: string): string {
  return heading
    .toLowerCase()
    .replace(/`/g, "")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 80);
}

interface Section {
  headingPath: string[];
  lines: string[];
}

export function chunkMarkdown(docId: string, content: string): { title: string | null; chunks: Chunk[] } {
  const lines = content.split("\n");
  let title: string | null = null;
  let inFence = false;
  let fenceMarker = "";
  let current: Section = { headingPath: [], lines: [] };
  const sections: Section[] = [current];
  let h2: string | null = null;

  for (const line of lines) {
    const fence = line.match(/^(\s*)(`{3,}|~{3,})/);
    const marker = fence?.[2];
    if (marker) {
      if (!inFence) {
        inFence = true;
        fenceMarker = (marker[0] ?? "`").repeat(3);
      } else if (marker.startsWith(fenceMarker)) {
        inFence = false;
      }
      current.lines.push(line);
      continue;
    }
    if (!inFence) {
      const h1m = line.match(/^#\s+(.*)/);
      if (h1m && title === null) {
        title = (h1m[1] ?? "").trim();
        current.lines.push(line);
        continue;
      }
      const h2m = line.match(/^##\s+(.*)/);
      const h3m = line.match(/^###\s+(.*)/);
      if (h2m) {
        h2 = (h2m[1] ?? "").trim();
        current = { headingPath: [h2], lines: [line] };
        sections.push(current);
        continue;
      }
      if (h3m) {
        const h3 = (h3m[1] ?? "").trim();
        current = { headingPath: h2 ? [h2, h3] : [h3], lines: [line] };
        sections.push(current);
        continue;
      }
    }
    current.lines.push(line);
  }

  // Merge tiny HEADING-LESS fragments forward (spec §5): a lone intro line
  // dilutes ranking without carrying an answer. Sections that own a heading
  // always stand alone, however short — a three-line "Deploy / rollback" is
  // exactly the answer unit an operator searches for, and merging it forward
  // would hand its lines to the NEXT section's heading_path (wrong provenance,
  // lost heading boost — caught by the test suite).
  const merged: Section[] = [];
  let carry: Section | null = null;
  for (const s of sections) {
    const text = s.lines.join("\n").trim();
    if (text.length === 0) continue;
    if (carry) {
      s.lines = [...carry.lines, ...s.lines];
      carry = null;
    }
    if (text.length < MIN_CHUNK_CHARS && s.headingPath.length === 0) {
      carry = s;
      continue;
    }
    merged.push(s);
  }
  if (carry) merged.push(carry); // trailing heading-less fragment: keep rather than drop

  // Oversize sections split at paragraph bounds with the heading repeated.
  const out: Chunk[] = [];
  const ordinals = new Map<string, number>();
  for (const s of merged) {
    const headingPath = s.headingPath.join(" > ");
    const text = s.lines.join("\n").trim();
    const parts: string[] = [];
    if (text.length <= MAX_CHUNK_CHARS) {
      parts.push(text);
    } else {
      const paras = text.split(/\n{2,}/);
      let buf = "";
      // Regenerate the heading from headingPath (never from lines[0] — a
      // merged-forward preamble fragment may sit above the heading line).
      const last = s.headingPath[s.headingPath.length - 1];
      const headerLine = last ? `${"#".repeat(s.headingPath.length === 1 ? 2 : 3)} ${last}\n\n` : "";
      for (const p of paras) {
        if (buf.length > 0 && buf.length + p.length > MAX_CHUNK_CHARS) {
          parts.push(buf);
          buf = headerLine + p;
        } else {
          buf = buf ? `${buf}\n\n${p}` : p;
        }
      }
      if (buf.trim()) parts.push(buf);
    }
    for (const part of parts) {
      const slug = s.headingPath.length ? s.headingPath.map(slugify).join("--") : "intro";
      const seen = ordinals.get(slug) ?? 0;
      ordinals.set(slug, seen + 1);
      // chunk_id is DECLARED UNSTABLE across doc edits (spec §5): the ordinal
      // shifts when a duplicate heading is inserted earlier. Durable citation
      // is doc_id + heading_path + source_commit.
      const chunkId = `${docId}#${slug}${seen > 0 ? `~${seen}` : ""}`;
      out.push({ chunkId, chunkIndex: out.length, headingPath, body: part });
    }
  }
  return { title, chunks: out };
}
