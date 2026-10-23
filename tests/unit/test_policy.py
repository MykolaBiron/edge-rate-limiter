from pathlib import Path

import pytest
from pydantic import ValidationError

from gateway.geo import GeoContext
from gateway.identity import ClientIdentity
from gateway.limiters import FixedWindow, SlidingWindow, TokenBucket
from gateway.policy import DEFAULT_ZONE, Match, PolicyConfig, PolicyResolver

ROOT = Path(__file__).resolve().parents[2]

IP_CLIENT = ClientIdentity(key="ip:1.2.3.4", ip_key="ip:1.2.3.4", ip="1.2.3.4", kind="ip")
KEY_CLIENT = ClientIdentity(key="k:abc", ip_key="ip:1.2.3.4", ip="1.2.3.4", kind="api_key")
NOWHERE = GeoContext()

CONFIG = {
    "zones": {
        "eu": {"countries": ["DE", "FR"], "upstream": "http://eu:1"},
        "west": {"countries": ["US-CA", "US-OR"], "upstream": "http://west:1"},
        "us": {"countries": ["US"]},
        "embargoed": {"countries": ["KP"], "action": "block", "status": 451},
        "tor": {"countries": ["T1"], "action": "block", "status": 403},
    },
    "policies": [
        {"name": "default", "strategy": {"type": "fixed_window", "limit": 100, "window_ms": 1000}},
        {
            "name": "api",
            "match": {"path_prefix": "/api"},
            "strategy": {"type": "token_bucket", "capacity": 10, "refill_rate": 1},
            "zone_overrides": {"eu": {"type": "token_bucket", "capacity": 5, "refill_rate": 1}},
        },
        {
            "name": "api-write",
            "match": {"path_prefix": "/api/", "methods": ["post", "PUT"]},
            "strategy": {"type": "fixed_window", "limit": 2, "window_ms": 1000},
        },
        {
            "name": "login",
            "match": {"path_prefix": "/login", "methods": ["POST"]},
            "identity": "ip",
            "strategy": {"type": "sliding_window", "limit": 5, "window_ms": 60000},
        },
    ],
}


@pytest.fixture
def resolver() -> PolicyResolver:
    return PolicyResolver(PolicyConfig.model_validate(CONFIG))


@pytest.mark.parametrize(
    ("prefix", "path", "matches"),
    [
        ("/", "/", True),
        ("/", "/anything/at/all", True),
        ("/api", "/api", True),
        ("/api", "/api/users", True),
        ("/api", "/apix", False),
        ("/api", "/ap", False),
        ("/api/", "/api/users", True),
        ("/api/", "/api", False),
        ("/api/v1", "/api/v1/x", True),
        ("/api/v1", "/api/v2/x", False),
    ],
)
def test_prefix_matching_respects_segments(prefix, path, matches):
    assert Match(path_prefix=prefix).matches(path, "GET") is matches


@pytest.mark.parametrize(("method", "matches"), [("GET", True), ("POST", False)])
def test_method_filter(method, matches):
    assert Match(path_prefix="/", methods=["get"]).matches("/", method) is matches


@pytest.mark.parametrize(
    ("path", "method", "policy"),
    [
        ("/", "GET", "default"),
        ("/other", "GET", "default"),
        ("/api", "GET", "api"),
        ("/api/users", "GET", "api"),
        ("/api/users", "POST", "api-write"),
        ("/api/users", "PUT", "api-write"),
        ("/api/users", "DELETE", "api"),
        ("/login", "POST", "login"),
        ("/login", "GET", "default"),
    ],
)
def test_longest_matching_policy_wins(resolver, path, method, policy):
    assert resolver.match(path, method).name == policy


@pytest.mark.parametrize(
    ("country", "region", "zone"),
    [
        ("DE", None, "eu"),
        ("FR", "IDF", "eu"),
        ("US", None, "us"),
        ("US", "CA", "west"),
        ("US", "TX", "us"),
        ("KP", None, "embargoed"),
        ("T1", None, "tor"),
        ("JP", None, DEFAULT_ZONE),
        (None, None, DEFAULT_ZONE),
    ],
)
def test_zone_mapping(resolver, country, region, zone):
    assert resolver.zone_for(GeoContext(country=country, region=region)) == zone


@pytest.mark.parametrize(("country", "status"), [("KP", 451), ("T1", 403)])
def test_blocked_zones(resolver, country, status):
    res = resolver.resolve("/api", "GET", GeoContext(country=country), IP_CLIENT)
    assert res.blocked
    assert res.block_status == status
    assert res.policy is None


def test_zone_override_replaces_strategy_and_scopes_state(resolver):
    eu = resolver.resolve("/api", "GET", GeoContext(country="DE"), IP_CLIENT)
    home = resolver.resolve("/api", "GET", NOWHERE, IP_CLIENT)
    assert eu.strategy == TokenBucket(capacity=5, refill_rate=1)
    assert home.strategy == TokenBucket(capacity=10, refill_rate=1)
    assert eu.limiter_key == "api@eu:ip:1.2.3.4"
    assert home.limiter_key == "api:ip:1.2.3.4"


def test_zone_without_override_uses_base_strategy(resolver):
    res = resolver.resolve("/", "GET", GeoContext(country="DE"), IP_CLIENT)
    assert res.policy.name == "default"
    assert res.limiter_key == "default:ip:1.2.3.4"


@pytest.mark.parametrize(
    ("country", "region", "upstream"),
    [
        ("DE", None, "http://eu:1"),
        ("US", "CA", "http://west:1"),
        ("US", None, None),
        (None, None, None),
    ],
)
def test_geo_routing_picks_zone_upstream(resolver, country, region, upstream):
    res = resolver.resolve("/", "GET", GeoContext(country=country, region=region), IP_CLIENT)
    assert res.upstream == upstream


def test_api_key_identity_used_by_default(resolver):
    assert resolver.resolve("/api", "GET", NOWHERE, KEY_CLIENT).limiter_key == "api:k:abc"


def test_ip_identity_policy_ignores_api_key(resolver):
    res = resolver.resolve("/login", "POST", NOWHERE, KEY_CLIENT)
    assert res.limiter_key == "login:ip:1.2.3.4"


def test_no_matching_policy_means_unlimited():
    resolver = PolicyResolver(
        PolicyConfig.model_validate(
            {
                "policies": [
                    {
                        "name": "only-api",
                        "match": {"path_prefix": "/api"},
                        "strategy": {"type": "fixed_window", "limit": 1, "window_ms": 1},
                    }
                ]
            }
        )
    )
    res = resolver.resolve("/public", "GET", NOWHERE, IP_CLIENT)
    assert res.policy is None and not res.blocked


def test_by_name_index(resolver):
    assert set(resolver.by_name) == {"default", "api", "api-write", "login"}


def test_shipped_policies_file_is_valid():
    resolver = PolicyResolver.from_file(ROOT / "policies.yaml")
    assert "NO" in resolver.config.zones["eu"].countries
    assert resolver.match("/auth/login", "POST").name == "auth-login"


def test_empty_file(tmp_path):
    path = tmp_path / "empty.yaml"
    path.write_text("")
    assert PolicyResolver.from_file(path).config.policies == []


def _policy(**overrides):
    base = {"name": "p", "strategy": {"type": "fixed_window", "limit": 5, "window_ms": 1000}}
    return {"policies": [{**base, **overrides}]}


@pytest.mark.parametrize(
    "config",
    [
        _policy(name="has space"),
        _policy(name="{brace}"),
        _policy(name=""),
        _policy(match={"path_prefix": "api"}),
        _policy(identity="cookie"),
        _policy(cost=0),
        _policy(cost=6),
        _policy(strategy={"type": "nope"}),
        _policy(strategy={"type": "fixed_window", "limit": 0, "window_ms": 1000}),
        _policy(zone_overrides={"mars": {"type": "fixed_window", "limit": 1, "window_ms": 1}}),
        _policy(unknown_field=True),
        {"policies": [_policy()["policies"][0], _policy()["policies"][0]]},
        {"zones": {"a": {"countries": ["DE"]}, "b": {"countries": ["de"]}}},
        {"zones": {"a": {"countries": []}}},
        {"zones": {"a": {"countries": ["DE"], "action": "block", "status": 500}}},
    ],
    ids=[
        "space-in-name",
        "brace-in-name",
        "empty-name",
        "relative-prefix",
        "bad-identity",
        "zero-cost",
        "cost-over-limit",
        "bad-strategy",
        "zero-limit",
        "unknown-zone-override",
        "extra-field",
        "duplicate-names",
        "country-in-two-zones",
        "empty-zone",
        "bad-block-status",
    ],
)
def test_invalid_configs_are_rejected(config):
    with pytest.raises(ValidationError):
        PolicyConfig.model_validate(config)


def test_cost_checked_against_zone_overrides_too():
    config = {
        "zones": {"eu": {"countries": ["DE"]}},
        "policies": [
            {
                "name": "p",
                "cost": 3,
                "strategy": {"type": "fixed_window", "limit": 5, "window_ms": 1000},
                "zone_overrides": {"eu": {"type": "fixed_window", "limit": 2, "window_ms": 1000}},
            }
        ],
    }
    with pytest.raises(ValidationError, match="exceeds limit"):
        PolicyConfig.model_validate(config)


def test_strategy_types_are_parsed():
    config = PolicyConfig.model_validate(CONFIG)
    assert isinstance(config.policies[0].strategy, FixedWindow)
    assert isinstance(config.policies[3].strategy, SlidingWindow)
