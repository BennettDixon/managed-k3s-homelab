"""The deployed registry parsed under the service's own admission rules (spec §4).

A malformed registry fails readiness on the pod, which freezes the Flux apps
chain (wait: true) — so this is the guard, and CI runs it on every PR that
touches the file. The deployed file lands with slice 2; until then the
example registry in this directory is what gets parsed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from gateway.registry import check_workspace_limit, parse_registry

SERVICE_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVICE_DIR.parents[1]
DEPLOYED = REPO_ROOT / "apps" / "base" / "gateway" / "registry.yaml"
EXAMPLE = SERVICE_DIR / "registry.example.yaml"


def test_example_registry_parses_with_the_spec_shape() -> None:
    registry = parse_registry(EXAMPLE.read_text())
    check_workspace_limit(registry)
    assert set(registry.projects) == {"homelab-ops", "gateway-smoke"}
    assert set(registry.callers) == {"operator", "n8n-executor"}
    assert registry.callers["n8n-executor"].class_ == "executor"
    assert registry.callers["n8n-executor"].projects == ("gateway-smoke",)
    assert registry.callers["n8n-executor"].max_request_cap_micro == 100_000
    assert registry.projects["gateway-smoke"].cap_micro == 1_000_000
    # v1 is metered-only: no project routes to the deferred lane by default.
    assert all(p.default_lane == "metered" for p in registry.projects.values())
    assert all(c.class_ != "lane-agent" for c in registry.callers.values())


@pytest.mark.skipif(not DEPLOYED.exists(), reason="apps/base/gateway/registry.yaml lands with slice 2")
def test_deployed_registry_parses_and_fits_the_workspace_limit() -> None:
    registry = parse_registry(DEPLOYED.read_text())
    check_workspace_limit(registry)
    assert "gateway-smoke" in registry.projects
    assert all(c.class_ != "lane-agent" for c in registry.callers.values())  # deferred lane


def test_public_repo_safety_of_shipped_files() -> None:
    # Four-octet forms only; the 100.64/10 CGNAT range is every tailnet IPv4,
    # fd7a:115c:a1e0 the ULA (mirrors knowledge-mcp's guard).
    private_or_tailnet = re.compile(
        r"\b(10(\.\d{1,3}){3}|192\.168(\.\d{1,3}){2}|172\.(1[6-9]|2\d|3[01])(\.\d{1,3}){2}"
        r"|100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])(\.\d{1,3}){2})\b|fd7a:115c:a1e0",
        re.IGNORECASE,
    )
    files = [
        p
        for p in SERVICE_DIR.rglob("*")
        if p.is_file()
        and ".venv" not in p.parts
        and "__pycache__" not in p.parts
        and not any(part.startswith(".") for part in p.relative_to(SERVICE_DIR).parts)
    ]
    if DEPLOYED.exists():
        files.extend(DEPLOYED.parent.iterdir())
    assert files
    fqdn_marker = ".ts" + ".net"  # built at runtime so this file does not trip its own scan
    for path in files:
        if path.suffix in {".db", ".lock", ".pyc"} or path == Path(__file__):
            continue  # the scanner carries the patterns it hunts for
        text = path.read_text(errors="ignore")
        assert fqdn_marker not in text, path
        assert not private_or_tailnet.search(text), path
