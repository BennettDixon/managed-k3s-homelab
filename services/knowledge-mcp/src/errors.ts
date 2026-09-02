// Error taxonomy per spec §3. Every tool error is {code, message, retryable}.
export type ErrorCode =
  | "E_UNAUTHORIZED"
  | "E_FORBIDDEN"
  | "E_SCHEMA"
  | "E_CORPUS_UNKNOWN"
  | "E_NOT_FOUND"
  | "E_URI_FORBIDDEN"
  | "E_URI_UNREACHABLE"
  | "E_DOC_TOO_LARGE"
  | "E_QUERY_INVALID"
  | "E_NO_REBUILD_SOURCE"
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
