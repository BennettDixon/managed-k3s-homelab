"""Error taxonomy (spec §3) in the OpenAI envelope.

``{"error": {"type", "code": "E_*", "message", "param", "retryable", ...}}``.
Every error response also carries ``x-should-retry`` — the OpenAI SDKs obey
that header (read from the SDK source, spec §13 item 8): a 402 is never
retried on its own, but 409 and 5xx would be, and a retried 409 or 504 is a
second spend the caller never chose.
"""

from __future__ import annotations

from collections.abc import Mapping

OPENAI_TYPE_BY_STATUS: dict[int, str] = {
    400: "invalid_request_error",
    401: "authentication_error",
    402: "insufficient_quota",
    403: "permission_error",
    404: "not_found_error",
    409: "invalid_request_error",
    413: "invalid_request_error",
    429: "rate_limit_error",
    500: "server_error",
    501: "invalid_request_error",
    502: "server_error",
    503: "server_error",
    504: "server_error",
}


class GatewayError(Exception):
    def __init__(
        self,
        code: str,
        status: int,
        message: str,
        *,
        param: str | None = None,
        retryable: bool = False,
        extra: Mapping[str, object] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.message = message
        self.param = param
        self.retryable = retryable
        self.extra: dict[str, object] = dict(extra or {})
        self.headers: dict[str, str] = dict(headers or {})

    def envelope(self) -> dict[str, object]:
        err: dict[str, object] = {
            "type": OPENAI_TYPE_BY_STATUS.get(self.status, "server_error"),
            "code": self.code,
            "message": self.message,
            "param": self.param,
            "retryable": self.retryable,
        }
        err.update(self.extra)
        return {"error": err}

    def response_headers(self) -> dict[str, str]:
        return {"x-should-retry": "true" if self.retryable else "false", **self.headers}


def unauthorized() -> GatewayError:
    return GatewayError("E_UNAUTHORIZED", 401, "missing or invalid bearer token")


def forbidden(message: str) -> GatewayError:
    return GatewayError("E_FORBIDDEN", 403, message)


def schema(message: str, param: str | None = None) -> GatewayError:
    return GatewayError("E_SCHEMA", 400, message, param=param)


def unsupported(message: str, param: str | None = None, status: int = 400) -> GatewayError:
    return GatewayError("E_UNSUPPORTED", status, message, param=param)


def not_found(message: str) -> GatewayError:
    return GatewayError("E_NOT_FOUND", 404, message)


def internal() -> GatewayError:
    # Details are logged, never leaked (spec §3).
    return GatewayError("E_INTERNAL", 500, "internal error", retryable=True)


def ledger_unavailable(message: str) -> GatewayError:
    return GatewayError("E_LEDGER_UNAVAILABLE", 503, message, retryable=True, headers={"retry-after": "5"})
