// Error taxonomy per spec §3. Every tool error is {code, message, retryable}.
export type ErrorCode =
  | "E_UNAUTHORIZED"
  | "E_FORBIDDEN"
  | "E_SCHEMA"
  | "E_NOT_FOUND" // covers unknown AND invisible corpora/docs — no existence oracle (spec §4)
  | "E_URI_FORBIDDEN"
  | "E_URI_UNREACHABLE"
  | "E_DOC_TOO_LARGE"
  | "E_QUERY_INVALID"
  | "E_NO_REBUILD_SOURCE" // reserved for the S4 push shape; unreachable in v1 (registry requires rebuild_source)
  | "E_UNSUPPORTED"
  | "E_INTERNAL";

export class KnowledgeError extends Error {
  constructor(
    public code: ErrorCode,
    message: string,
    public retryable = false,
  ) {
    super(message);
  }
  toJSON() {
    return { code: this.code, message: this.message, retryable: this.retryable };
  }
}
