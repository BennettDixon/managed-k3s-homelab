"""Registry (spec §4): models, prices, aliases, projects, callers — with admission.

Repo-owned ConfigMap; parse failure fails readiness. Every admission rule is
enforced at parse time so a bad PR can never half-apply, and the same parser
runs in CI against the deployed file (tests/test_registry_manifest.py).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from gateway.ids import ID_RE
from gateway.money import MoneyError, Prices, usd_to_micro

Lane = Literal["metered", "subscription"]
LANES: tuple[str, ...] = ("metered", "subscription")
CallerClass = Literal["operator", "executor", "worker", "lane-agent"]
# The class enum is closed at admission (spec §2): there is no `frontend`
# value and no way to declare one.
SPENDING_CLASSES: frozenset[str] = frozenset({"operator", "executor", "worker"})
PROJECT_CAP_MAX_MICRO = 10_000 * 1_000_000


class RegistryError(ValueError):
    pass


def _to_decimal(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        raise ValueError("must be a number")
    if isinstance(value, float):
        return Decimal(repr(value))
    return Decimal(value)


class _ModelYaml(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    input: Decimal
    output: Decimal
    cache_write_5m: Decimal
    cache_write_1h: Decimal
    cache_read: Decimal
    max_tokens: int = Field(gt=0)
    default_max_tokens: int = Field(gt=0)
    effort: bool
    lanes: list[Lane] = Field(min_length=1)
    cli_alias: str | None = None

    @field_validator("input", "output", "cache_write_5m", "cache_write_1h", "cache_read", mode="before")
    @classmethod
    def _price(cls, value: object) -> Decimal:
        return _to_decimal(value)


class _ProjectYaml(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    cap_usd: Decimal
    period: Literal["month"]
    lanes: list[Lane] = Field(min_length=1)
    default_lane: Lane
    fallback_allowed: bool = False
    max_request_cap_usd: Decimal
    default_model: str
    models: list[str] = Field(min_length=1)

    @field_validator("cap_usd", "max_request_cap_usd", mode="before")
    @classmethod
    def _usd(cls, value: object) -> Decimal:
        return _to_decimal(value)


class _CallerYaml(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)

    class_: CallerClass = Field(alias="class")
    projects: list[str] | None = None
    lanes: list[Lane] | None = None
    max_request_cap_usd: Decimal | None = None
    # Per-caller daily billed ceiling — the `caller_day` scope of spec §3's
    # refusal enum (§15 tail: "$5 billed per day" on the operator's token).
    max_day_billed_usd: Decimal | None = None
    lane: Lane | None = None

    @field_validator("max_request_cap_usd", "max_day_billed_usd", mode="before")
    @classmethod
    def _usd(cls, value: object) -> Decimal | None:
        if value is None:
            return None
        return _to_decimal(value)


class _RegistryYaml(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    prices_as_of: date
    console_workspace_limit_usd: Decimal
    models: dict[str, _ModelYaml] = Field(min_length=1)
    aliases: dict[str, str] = Field(default_factory=dict)
    projects: dict[str, _ProjectYaml] = Field(min_length=1)
    callers: dict[str, _CallerYaml] = Field(min_length=1)

    @field_validator("console_workspace_limit_usd", mode="before")
    @classmethod
    def _usd(cls, value: object) -> Decimal:
        return _to_decimal(value)

    @field_validator("prices_as_of", mode="before")
    @classmethod
    def _date(cls, value: object) -> date:
        if isinstance(value, datetime):
            raise ValueError("prices_as_of must be a date, not a datetime")
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            return date.fromisoformat(value)
        raise ValueError("prices_as_of must be an ISO date")


@dataclass(frozen=True)
class Model:
    id: str
    prices: Prices
    max_tokens: int
    default_max_tokens: int
    effort: bool
    lanes: frozenset[str]
    cli_alias: str | None


@dataclass(frozen=True)
class Project:
    id: str
    cap_micro: int
    period: str
    lanes: frozenset[str]
    default_lane: str
    fallback_allowed: bool
    max_request_cap_micro: int
    default_model: str
    models: tuple[str, ...]


@dataclass(frozen=True)
class Caller:
    id: str
    class_: str
    projects: tuple[str, ...]
    lanes: frozenset[str]
    max_request_cap_micro: int | None
    max_day_billed_micro: int | None
    lane: str | None


@dataclass(frozen=True)
class Registry:
    prices_as_of: date
    console_workspace_limit_micro: int
    models: dict[str, Model]
    aliases: dict[str, str]
    projects: dict[str, Project]
    callers: dict[str, Caller]
    prices_hash: str
    empty: bool = field(default=False)

    @staticmethod
    def none() -> Registry:
        """The registry a pod runs with when the ConfigMap failed to parse: nobody can do anything."""
        return Registry(
            prices_as_of=date(1970, 1, 1),
            console_workspace_limit_micro=0,
            models={},
            aliases={},
            projects={},
            callers={},
            prices_hash="",
            empty=True,
        )


def _price_micro(model_id: str, name: str, value: Decimal) -> int:
    if value <= 0:
        raise RegistryError(f"model {model_id}: price {name} must be > 0")
    try:
        micro = usd_to_micro(value)
    except MoneyError as err:
        raise RegistryError(f"model {model_id}: price {name} is not representable in micro-USD") from err
    return micro


def _usd_micro(where: str, name: str, value: Decimal) -> int:
    try:
        return usd_to_micro(value)
    except MoneyError as err:
        raise RegistryError(f"{where}: {name} is not representable in micro-USD") from err


def _check_id(kind: str, ident: str) -> None:
    if not ID_RE.match(ident):
        raise RegistryError(f"{kind} id {ident!r} must match {ID_RE.pattern}")


def parse_registry(text: str, *, max_request_cap_micro: int = 25_000_000, today: date | None = None) -> Registry:
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as err:
        raise RegistryError(f"registry is not valid YAML: {err}") from err
    if not isinstance(loaded, dict):
        raise RegistryError("registry must be a YAML mapping")
    try:
        raw = _RegistryYaml.model_validate(loaded)
    except ValidationError as err:
        first = err.errors()[0]
        loc = ".".join(str(p) for p in first["loc"])
        raise RegistryError(f"registry schema: {loc}: {first['msg']}") from err

    today = today or datetime.now(UTC).date()
    if raw.prices_as_of > today:
        raise RegistryError(f"prices_as_of {raw.prices_as_of} is in the future")

    models: dict[str, Model] = {}
    for model_id, m in raw.models.items():
        _check_id("model", model_id)
        prices = Prices(
            input=_price_micro(model_id, "input", m.input),
            output=_price_micro(model_id, "output", m.output),
            cache_write_5m=_price_micro(model_id, "cache_write_5m", m.cache_write_5m),
            cache_write_1h=_price_micro(model_id, "cache_write_1h", m.cache_write_1h),
            cache_read=_price_micro(model_id, "cache_read", m.cache_read),
        )
        # Catches transposed columns (spec §4).
        if not prices.cache_read <= prices.input <= prices.cache_write_5m <= prices.cache_write_1h:
            raise RegistryError(
                f"model {model_id}: prices must satisfy cache_read <= input <= cache_write_5m <= cache_write_1h"
            )
        if m.default_max_tokens > m.max_tokens:
            raise RegistryError(f"model {model_id}: default_max_tokens exceeds max_tokens")
        if len(set(m.lanes)) != len(m.lanes):
            raise RegistryError(f"model {model_id}: duplicate lane")
        if "subscription" in m.lanes and not m.cli_alias:
            raise RegistryError(f"model {model_id}: a subscription-lane model needs cli_alias")
        models[model_id] = Model(
            id=model_id,
            prices=prices,
            max_tokens=m.max_tokens,
            default_max_tokens=m.default_max_tokens,
            effort=m.effort,
            lanes=frozenset(m.lanes),
            cli_alias=m.cli_alias,
        )

    aliases: dict[str, str] = {}
    for alias, target in raw.aliases.items():
        _check_id("alias", alias)
        if alias in models:
            raise RegistryError(f"alias {alias} collides with a model id")
        if target not in models:
            raise RegistryError(f"alias {alias}: unknown model {target}")
        aliases[alias] = target

    projects: dict[str, Project] = {}
    for project_id, p in raw.projects.items():
        _check_id("project", project_id)
        cap = _usd_micro(f"project {project_id}", "cap_usd", p.cap_usd)
        if not 0 <= cap <= PROJECT_CAP_MAX_MICRO:
            raise RegistryError(f"project {project_id}: cap_usd must be in [0, 10000]")
        if len(set(p.lanes)) != len(p.lanes):
            raise RegistryError(f"project {project_id}: duplicate lane")
        if p.default_lane not in p.lanes:
            raise RegistryError(f"project {project_id}: default_lane {p.default_lane} is not in lanes")
        for model_id in p.models:
            model = models.get(model_id)
            if model is None:
                raise RegistryError(f"project {project_id}: unknown model {model_id}")
            missing = set(p.lanes) - model.lanes
            if missing:
                raise RegistryError(f"project {project_id}: model {model_id} does not serve lane(s) {sorted(missing)}")
        if len(set(p.models)) != len(p.models):
            raise RegistryError(f"project {project_id}: duplicate model")
        if p.default_model not in p.models:
            raise RegistryError(f"project {project_id}: default_model {p.default_model} is not in models")
        max_req = _usd_micro(f"project {project_id}", "max_request_cap_usd", p.max_request_cap_usd)
        if max_req < 0 or max_req > cap:
            raise RegistryError(f"project {project_id}: max_request_cap_usd must be in [0, cap_usd]")
        if p.fallback_allowed and "metered" not in p.lanes:
            raise RegistryError(f"project {project_id}: fallback_allowed requires the metered lane")
        projects[project_id] = Project(
            id=project_id,
            cap_micro=cap,
            period=p.period,
            lanes=frozenset(p.lanes),
            default_lane=p.default_lane,
            fallback_allowed=p.fallback_allowed,
            max_request_cap_micro=max_req,
            default_model=p.default_model,
            models=tuple(p.models),
        )

    callers: dict[str, Caller] = {}
    for caller_id, c in raw.callers.items():
        _check_id("caller", caller_id)
        if c.class_ in SPENDING_CLASSES:
            if c.lane is not None:
                raise RegistryError(f"caller {caller_id}: `lane` is only for class lane-agent")
            if c.max_request_cap_usd is None:
                raise RegistryError(f"caller {caller_id}: max_request_cap_usd is required for class {c.class_}")
            max_req = _usd_micro(f"caller {caller_id}", "max_request_cap_usd", c.max_request_cap_usd)
            if max_req <= 0 or max_req > max_request_cap_micro:
                raise RegistryError(
                    f"caller {caller_id}: max_request_cap_usd must be in "
                    f"(0, MAX_REQUEST_CAP_USD={max_request_cap_micro / 1e6}]"
                )
            if not c.projects:
                raise RegistryError(f"caller {caller_id}: a spending caller needs non-empty projects")
            if len(set(c.projects)) != len(c.projects):
                raise RegistryError(f"caller {caller_id}: duplicate project")
            for project_id in c.projects:
                if project_id not in projects:
                    raise RegistryError(f"caller {caller_id}: unknown project {project_id}")
            if not c.lanes:
                raise RegistryError(f"caller {caller_id}: lanes must be declared explicitly")
            lanes = frozenset(c.lanes)
            for project_id in c.projects:
                extra = lanes - projects[project_id].lanes
                if extra:
                    raise RegistryError(
                        f"caller {caller_id}: lane(s) {sorted(extra)} are not granted by project {project_id}"
                    )
            # The subscription lane is granted only by an explicit registry line
            # and, in v1, only to class operator (spec §4; SIGN-OFF 9).
            if "subscription" in lanes and c.class_ != "operator":
                raise RegistryError(f"caller {caller_id}: the subscription lane is operator-only in v1")
            max_day: int | None = None
            if c.max_day_billed_usd is not None:
                max_day = _usd_micro(f"caller {caller_id}", "max_day_billed_usd", c.max_day_billed_usd)
                if max_day <= 0:
                    raise RegistryError(f"caller {caller_id}: max_day_billed_usd must be > 0")
            callers[caller_id] = Caller(
                id=caller_id,
                class_=c.class_,
                projects=tuple(c.projects),
                lanes=lanes,
                max_request_cap_micro=max_req,
                max_day_billed_micro=max_day,
                lane=None,
            )
        else:  # lane-agent
            if c.lane is None:
                raise RegistryError(f"caller {caller_id}: class lane-agent requires `lane`")
            if c.projects or c.lanes or c.max_request_cap_usd is not None or c.max_day_billed_usd is not None:
                raise RegistryError(f"caller {caller_id}: class lane-agent carries only `lane` (it can never spend)")
            callers[caller_id] = Caller(
                id=caller_id,
                class_=c.class_,
                projects=(),
                lanes=frozenset(),
                max_request_cap_micro=None,
                max_day_billed_micro=None,
                lane=c.lane,
            )

    limit = _usd_micro("registry", "console_workspace_limit_usd", raw.console_workspace_limit_usd)
    if limit <= 0:
        raise RegistryError("console_workspace_limit_usd must be > 0")

    return Registry(
        prices_as_of=raw.prices_as_of,
        console_workspace_limit_micro=limit,
        models=models,
        aliases=aliases,
        projects=projects,
        callers=callers,
        prices_hash=_prices_hash(models),
    )


def _prices_hash(models: dict[str, Model]) -> str:
    canonical = {
        model_id: {
            "input": m.prices.input,
            "output": m.prices.output,
            "cache_write_5m": m.prices.cache_write_5m,
            "cache_write_1h": m.prices.cache_write_1h,
            "cache_read": m.prices.cache_read,
        }
        for model_id, m in sorted(models.items())
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def check_workspace_limit(registry: Registry) -> None:
    """``Σ cap_usd <= console_workspace_limit_usd`` — a CI check, never a readiness check (spec §4)."""
    total = sum(p.cap_micro for p in registry.projects.values())
    if total > registry.console_workspace_limit_micro:
        raise RegistryError(
            f"sum of project caps ({total / 1e6} USD) exceeds console_workspace_limit_usd "
            f"({registry.console_workspace_limit_micro / 1e6} USD)"
        )


def load_registry(path: str, *, max_request_cap_micro: int = 25_000_000) -> Registry:
    with open(path, encoding="utf-8") as handle:
        return parse_registry(handle.read(), max_request_cap_micro=max_request_cap_micro)


def resolve_model(registry: Registry, name: str) -> Model | None:
    model_id = registry.aliases.get(name, name)
    return registry.models.get(model_id)
