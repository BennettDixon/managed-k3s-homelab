"""Caller-token map resolution (spec §2) — port of knowledge-mcp's auth.ts.

The check runs constant-time per candidate AND iterates every candidate
regardless of an early match, so response timing never reveals which caller
id (if any) a probed token belongs to.
"""

from __future__ import annotations

import hmac
from collections.abc import Mapping

from gateway.registry import Caller, Registry


def resolve_caller(authorization: str | None, tokens: Mapping[str, str], registry: Registry) -> Caller | None:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    presented = authorization[len("Bearer ") :].encode("utf-8")
    matched: str | None = None
    for caller_id, token in tokens.items():
        if hmac.compare_digest(presented, token.encode("utf-8")):
            matched = caller_id
    if matched is None:
        return None
    # A token whose caller id has no registry policy entry authenticates
    # NOBODY: the secret map and the repo registry must agree (fail closed —
    # a secret-only caller would otherwise bypass the PR-reviewed policy).
    return registry.callers.get(matched)
