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
import yaml

from gateway import __version__
from gateway.config import load_config
from gateway.money import usd_to_micro
from gateway.registry import check_workspace_limit, parse_registry

SERVICE_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVICE_DIR.parents[1]
MANIFEST_DIR = REPO_ROOT / "apps" / "base" / "gateway"
DEPLOYED = MANIFEST_DIR / "registry.yaml"
DEPLOYMENT = MANIFEST_DIR / "deployment.yaml"
EXAMPLE = SERVICE_DIR / "registry.example.yaml"


def _container() -> dict[str, object]:
    """The gateway container of the deployed Deployment (slice 2 manifest)."""
    docs = [d for d in yaml.safe_load_all(DEPLOYMENT.read_text()) if d]
    deployment = next(d for d in docs if d.get("kind") == "Deployment")
    containers = deployment["spec"]["template"]["spec"]["containers"]
    assert len(containers) == 1
    container: dict[str, object] = containers[0]
    return container


def _deployment_env() -> dict[str, str]:
    """Literal env values of the deployed container (secretKeyRef entries are absent)."""
    env = _container()["env"]
    assert isinstance(env, list)
    return {e["name"]: e["value"] for e in env if "value" in e}


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
    # Parsed against the DEPLOYMENT's MAX_REQUEST_CAP_USD, not the code
    # default: the pod boots with the manifest's ceiling, and a caller whose
    # max_request_cap_usd exceeded it would fail readiness there, not here.
    env = _deployment_env()
    registry = parse_registry(DEPLOYED.read_text(), max_request_cap_micro=usd_to_micro(env["MAX_REQUEST_CAP_USD"]))
    check_workspace_limit(registry)
    assert "gateway-smoke" in registry.projects
    assert all(c.class_ != "lane-agent" for c in registry.callers.values())  # deferred lane
    # Spec §4 / §15 tail: the numbers the operator signed for slice 2.
    assert registry.projects["homelab-ops"].cap_micro == 20_000_000
    assert registry.projects["gateway-smoke"].cap_micro == 1_000_000
    assert registry.callers["operator"].max_request_cap_micro == 5_000_000
    assert registry.callers["operator"].max_day_billed_micro == 5_000_000
    assert registry.callers["n8n-executor"].max_request_cap_micro == 100_000
    assert registry.callers["n8n-executor"].projects == ("gateway-smoke",)
    # v1 is metered-only (SIGN-OFF 9): no project may route to or fall back
    # onto the deferred lane.
    assert all(p.lanes == frozenset({"metered"}) and not p.fallback_allowed for p in registry.projects.values())


@pytest.mark.skipif(not DEPLOYMENT.exists(), reason="apps/base/gateway/deployment.yaml lands with slice 2")
def test_deployment_env_boots_under_the_real_config_parser() -> None:
    """The manifest's numbers pass config.py's loud validation (a typo would fail the pod at boot)."""
    env = _deployment_env()
    config = load_config(
        {**env, "GATEWAY_CALLER_TOKENS": '{"operator": "0123456789abcdef0123"}', "ANTHROPIC_API_KEY": "test-key"}
    )
    assert config.port == 8080
    assert config.max_request_cap_micro == usd_to_micro(env["MAX_REQUEST_CAP_USD"])
    # Spec §8: the two brakes and the provider timeout are set explicitly.
    for name in (
        "GATEWAY_METERED_DAILY_CEILING_USD",
        "GATEWAY_SUBSCRIPTION_DAILY_LIST_CEILING_USD",
        "PROVIDER_TIMEOUT_MS",
        "LANE_PROBE_INTERVAL_MS",
    ):
        assert name in env, name
    assert config.provider_timeout_ms < 600_000  # under the server timeout (spec §2)
    assert config.lane_probe_interval_ms > 0  # the idle probe is on
    # The multiplier stays at the code default until usage.inference_geo is verified (spec §5).
    assert "BILLED_PRICE_MULTIPLIER_PCT" not in env
    # Never a forbidden ambient SDK variable in the pod env (spec §6.1).
    assert (
        not {"ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE", "ANTHROPIC_CUSTOM_HEADERS"} & env.keys()
    )


@pytest.mark.skipif(not DEPLOYMENT.exists(), reason="apps/base/gateway/deployment.yaml lands with slice 2")
def test_deployment_paths_tag_and_probes_agree_with_the_image() -> None:
    container = _container()
    env = _deployment_env()
    mounts = container["volumeMounts"]
    assert isinstance(mounts, list)
    mount_paths = {m["name"]: m["mountPath"] for m in mounts}
    # DB_PATH / REGISTRY_PATH are pinned in the manifest; this is what makes
    # that a single, CI-checked source of truth instead of a second one.
    assert env["DB_PATH"].startswith(mount_paths["data"] + "/")
    assert env["REGISTRY_PATH"] == mount_paths["registry"] + "/registry.yaml"
    assert mount_paths["tmp"] == "/tmp"
    # The tag moves with pyproject/__init__ (version-bump guard); on main the
    # three must already agree.
    assert container["image"] == f"harbor.internal/gateway/gateway:{__version__}"
    # Probe paths are the ones http.py serves; liveness never hits the write-ping.
    probes: dict[str, str] = {}
    for kind in ("startupProbe", "livenessProbe", "readinessProbe"):
        probe = container[kind]
        assert isinstance(probe, dict)
        probes[kind] = probe["httpGet"]["path"]
    assert probes == {"startupProbe": "/healthz", "livenessProbe": "/healthz", "readinessProbe": "/readyz"}
    port = env["PORT"]
    ports = container["ports"]
    assert isinstance(ports, list)
    assert ports[0]["containerPort"] == int(port)


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
        files.extend(MANIFEST_DIR.iterdir())
    assert files
    fqdn_marker = ".ts" + ".net"  # built at runtime so this file does not trip its own scan
    substitution_marker = "$" + "{"  # Flux postBuild envsubst runs over apps/homelab-prod (spec §8)
    for path in files:
        if path.suffix in {".db", ".lock", ".pyc"} or path == Path(__file__):
            continue  # the scanner carries the patterns it hunts for
        text = path.read_text(errors="ignore")
        assert fqdn_marker not in text, path
        assert not private_or_tailnet.search(text), path
        if path.parent == MANIFEST_DIR and path.suffix == ".yaml":  # what kustomize ships; the README may say it
            assert substitution_marker not in text, path
