import ipaddress

import pytest

from gateway.identity import (
    TrustedProxies,
    hash_api_key,
    identify,
    ip_bucket,
    parse_ip,
    resolve_client_ip,
)

TRUSTED = TrustedProxies(["10.0.0.0/8", "127.0.0.1/32", "fd00::/8"])


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1.2.3.4", "1.2.3.4"),
        (" 1.2.3.4 ", "1.2.3.4"),
        ("1.2.3.4:5678", "1.2.3.4"),
        ("2001:db8::1", "2001:db8::1"),
        ("[2001:db8::1]:443", "2001:db8::1"),
        ("[2001:db8::1]", "2001:db8::1"),
        ("::ffff:1.2.3.4", "1.2.3.4"),
        ("unknown", None),
        ("", None),
        (None, None),
        ("999.1.1.1", None),
        ("1.2.3", None),
        ("[garbage", None),
    ],
)
def test_parse_ip(raw, expected):
    parsed = parse_ip(raw)
    assert (str(parsed) if parsed else None) == expected


@pytest.mark.parametrize(
    ("ip", "trusted"),
    [
        ("10.1.2.3", True),
        ("127.0.0.1", True),
        ("fd12::1", True),
        ("8.8.8.8", False),
        ("11.0.0.1", False),
        ("2001:db8::1", False),
    ],
)
def test_trusted_proxies_membership(ip, trusted):
    assert (ipaddress.ip_address(ip) in TRUSTED) is trusted


def test_none_is_never_trusted():
    assert None not in TRUSTED


def test_untrusted_peer_cannot_spoof_forwarded_for():
    ip = resolve_client_ip("8.8.8.8", {"x-forwarded-for": "1.1.1.1"}, TRUSTED)
    assert str(ip) == "8.8.8.8"


def test_untrusted_peer_cannot_spoof_cdn_header():
    ip = resolve_client_ip("8.8.8.8", {"cf-connecting-ip": "1.1.1.1"}, TRUSTED)
    assert str(ip) == "8.8.8.8"


def test_trusted_peer_forwarded_for_single_hop():
    ip = resolve_client_ip("10.0.0.1", {"x-forwarded-for": "203.0.113.9"}, TRUSTED)
    assert str(ip) == "203.0.113.9"


@pytest.mark.parametrize(
    ("xff", "expected"),
    [
        # client-written values to the left of the first untrusted hop are ignored
        ("6.6.6.6, 203.0.113.9", "203.0.113.9"),
        ("6.6.6.6, 203.0.113.9, 10.0.0.5", "203.0.113.9"),
        ("203.0.113.9, 10.0.0.5, 10.0.0.6", "203.0.113.9"),
        # every hop is a proxy: fall back to the leftmost one reached
        ("10.0.0.7, 10.0.0.5", "10.0.0.7"),
        # a malformed hop stops the walk at the last trusted address
        ("203.0.113.9, garbage, 10.0.0.5", "10.0.0.5"),
        ("garbage", "10.0.0.1"),
        ("", "10.0.0.1"),
    ],
)
def test_forwarded_for_walks_right_to_left(xff, expected):
    assert str(resolve_client_ip("10.0.0.1", {"x-forwarded-for": xff}, TRUSTED)) == expected


@pytest.mark.parametrize("header", ["cf-connecting-ip", "true-client-ip", "fastly-client-ip"])
def test_cdn_client_ip_headers_from_trusted_peer(header):
    ip = resolve_client_ip("10.0.0.1", {header: "198.51.100.4"}, TRUSTED)
    assert str(ip) == "198.51.100.4"


def test_cdn_header_takes_precedence_over_forwarded_for():
    headers = {"cf-connecting-ip": "198.51.100.4", "x-forwarded-for": "203.0.113.9"}
    assert str(resolve_client_ip("10.0.0.1", headers, TRUSTED)) == "198.51.100.4"


def test_invalid_cdn_header_falls_through():
    headers = {"cf-connecting-ip": "nope", "x-forwarded-for": "203.0.113.9"}
    assert str(resolve_client_ip("10.0.0.1", headers, TRUSTED)) == "203.0.113.9"


def test_missing_peer():
    assert resolve_client_ip(None, {"x-forwarded-for": "1.1.1.1"}, TRUSTED) is None


@pytest.mark.parametrize(
    ("ip", "prefix", "expected"),
    [
        ("2001:db8:1:2:3:4:5:6", 64, "2001:db8:1:2::/64"),
        ("2001:db8:1:2:ffff::1", 64, "2001:db8:1:2::/64"),
        ("2001:db8:1:2:3:4:5:6", 48, "2001:db8:1::/48"),
        ("2001:db8:1:2:3:4:5:6", 128, "2001:db8:1:2:3:4:5:6"),
        ("192.0.2.1", 64, "192.0.2.1"),
    ],
)
def test_ip_bucket(ip, prefix, expected):
    assert ip_bucket(ipaddress.ip_address(ip), prefix) == expected


def test_ipv6_hosts_in_same_prefix_share_identity():
    a = identify("2001:db8::1", {}, TRUSTED)
    b = identify("2001:db8::ffff", {}, TRUSTED)
    assert a.key == b.key


def test_api_key_identity():
    client = identify("8.8.8.8", {"x-api-key": "secret"}, TRUSTED)
    assert client.kind == "api_key"
    assert client.key.startswith("k:")
    assert "secret" not in client.key
    assert client.ip_key == "ip:8.8.8.8"


def test_ip_identity():
    client = identify("8.8.8.8", {}, TRUSTED)
    assert client.kind == "ip"
    assert client.key == client.ip_key == "ip:8.8.8.8"


def test_unknown_ip_identity():
    assert identify(None, {}, TRUSTED).key == "ip:unknown"


@pytest.mark.parametrize("key", ["a", "x" * 4096, "{tag}", "has spaces", "ключ"])
def test_hashed_api_keys_are_safe_redis_key_parts(key):
    hashed = hash_api_key(key)
    assert len(hashed) == 24
    assert all(c in "0123456789abcdef" for c in hashed)


def test_api_key_hash_is_stable_and_distinct():
    assert hash_api_key("a") == hash_api_key("a")
    assert hash_api_key("a") != hash_api_key("b")
