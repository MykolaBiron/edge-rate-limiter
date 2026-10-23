from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, PositiveInt, field_validator, model_validator

from .geo import GeoContext
from .identity import ClientIdentity
from .limiters import Strategy, StrategyBase

NAME_PATTERN = r"^[A-Za-z0-9_.-]+$"
DEFAULT_ZONE = "default"


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Zone(_Model):
    # "DE" matches the whole country, "US-CA" only that region.
    countries: list[str] = Field(min_length=1)
    action: Literal["allow", "block"] = "allow"
    status: Literal[403, 451] = 451
    upstream: str | None = None

    @field_validator("countries")
    @classmethod
    def _upper(cls, value: list[str]) -> list[str]:
        return [c.strip().upper() for c in value]


class Match(_Model):
    path_prefix: str = "/"
    methods: list[str] | None = None

    @field_validator("path_prefix")
    @classmethod
    def _leading_slash(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("path_prefix must start with '/'")
        return value

    @field_validator("methods")
    @classmethod
    def _upper(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else [m.upper() for m in value]

    def matches(self, path: str, method: str) -> bool:
        if self.methods is not None and method not in self.methods:
            return False
        prefix = self.path_prefix
        if prefix == "/" or path == prefix:
            return True
        return path.startswith(prefix if prefix.endswith("/") else prefix + "/")


class Policy(_Model):
    name: str = Field(pattern=NAME_PATTERN)
    match: Match = Match()
    # "client" = API key when present, else IP. "ip" ignores API keys, which is what
    # you want for brute-force protection on login endpoints.
    identity: Literal["client", "ip"] = "client"
    strategy: Strategy
    zone_overrides: dict[str, Strategy] = {}
    cost: PositiveInt = 1
    # Hold admitted requests for the leaky-bucket delay instead of forwarding at once.
    shape: bool = False

    @model_validator(mode="after")
    def _cost_fits(self) -> "Policy":
        for strategy in (self.strategy, *self.zone_overrides.values()):
            if self.cost > strategy.max_cost:
                raise ValueError(
                    f"policy {self.name!r}: cost {self.cost} exceeds limit {strategy.max_cost}"
                )
        return self


class PolicyConfig(_Model):
    zones: dict[str, Zone] = {}
    policies: list[Policy] = []

    @model_validator(mode="after")
    def _check_references(self) -> "PolicyConfig":
        names = [p.name for p in self.policies]
        if len(names) != len(set(names)):
            raise ValueError("policy names must be unique")
        seen: dict[str, str] = {}
        for zone_name, zone in self.zones.items():
            for code in zone.countries:
                if code in seen:
                    raise ValueError(f"{code} is in both {seen[code]!r} and {zone_name!r}")
                seen[code] = zone_name
        for policy in self.policies:
            unknown = set(policy.zone_overrides) - set(self.zones)
            if unknown:
                raise ValueError(f"policy {policy.name!r} overrides unknown zones {unknown}")
        return self


@dataclass(frozen=True, slots=True)
class Resolution:
    zone: str
    blocked: bool = False
    block_status: int = 451
    policy: Policy | None = None
    strategy: StrategyBase | None = None
    limiter_key: str | None = None
    upstream: str | None = None


class PolicyResolver:
    def __init__(self, config: PolicyConfig) -> None:
        self.config = config
        self.by_name = {p.name: p for p in config.policies}
        # Longest prefix first, so specific routes win over catch-alls.
        self._policies = sorted(
            config.policies, key=lambda p: len(p.match.path_prefix), reverse=True
        )
        self._zone_by_code = {
            code: name for name, zone in config.zones.items() for code in zone.countries
        }

    @classmethod
    def from_file(cls, path: str | Path) -> "PolicyResolver":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls(PolicyConfig.model_validate(data))

    def zone_for(self, geo: GeoContext) -> str:
        if geo.country is None:
            return DEFAULT_ZONE
        if geo.region is not None:
            zone = self._zone_by_code.get(f"{geo.country}-{geo.region}")
            if zone is not None:
                return zone
        return self._zone_by_code.get(geo.country, DEFAULT_ZONE)

    def match(self, path: str, method: str) -> Policy | None:
        for policy in self._policies:
            if policy.match.matches(path, method):
                return policy
        return None

    def resolve(
        self, path: str, method: str, geo: GeoContext, client: ClientIdentity
    ) -> Resolution:
        zone_name = self.zone_for(geo)
        zone = self.config.zones.get(zone_name)
        if zone is not None and zone.action == "block":
            return Resolution(zone=zone_name, blocked=True, block_status=zone.status)

        upstream = zone.upstream if zone is not None else None
        policy = self.match(path, method)
        if policy is None:
            return Resolution(zone=zone_name, upstream=upstream)

        client_key = client.ip_key if policy.identity == "ip" else client.key

        override = policy.zone_overrides.get(zone_name)
        # Overrides get their own state so moving between zones can't reuse a bucket
        # built for different limits.
        scope = f"{policy.name}@{zone_name}" if override is not None else policy.name
        return Resolution(
            zone=zone_name,
            policy=policy,
            strategy=override or policy.strategy,
            limiter_key=f"{scope}:{client_key}",
            upstream=upstream,
        )
