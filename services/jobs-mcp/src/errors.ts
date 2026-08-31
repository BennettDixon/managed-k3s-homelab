// Error taxonomy per spec §3. Every tool error is {code, message, retryable}.
export type ErrorCode =
  | "E_UNAUTHORIZED"
  | "E_SCHEMA"
  | "E_ENVELOPE_VERSION"
  | "E_TASK_TYPE_UNKNOWN"
  | "E_PAYLOAD_INVALID"
  | "E_BUDGET_CAP_MISSING"
  | "E_BUDGET_CAP_INVALID"
  | "E_CONFLICT_IDEMPOTENCY"
  | "E_NOT_FOUND"
  | "E_NOT_CANCELABLE"
  | "E_INTERNAL";

export class JobsError extends Error {
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
