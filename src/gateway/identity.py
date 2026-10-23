import hashlib
import ipaddress
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

API_KEY_HEADER = "x-api-key"
# Set by CDNs to the address they saw; only honoured from trusted peers.
CDN_CLIENT_IP_HEADERS = ("cf-connecting-ip", "true-client-ip", "fastly-client-ip")


@dataclass(frozen=True, slots=True)
class ClientIdentity:
    key: str
    ip_key: str
    ip: str | None
    kind: Literal["api_key", "ip"]


class TrustedProxies:
    def __init__(self, cidrs: Iterable[str]) -> None:
        self._networks: tuple[IPNetwork, ...] = tuple(
            ipaddress.ip_network(c, strict=False) for c in cidrs
        )

    def __contains__(self, ip: IPAddress | None) -> bool:
        return ip is not None and any(ip in net for net in self._networks)


def parse_ip(value: str | None) -> IPAddress | None:
    if not value:
        return None
    value = value.strip()
    # Some proxies forward "[v6]:port" or "v4:port".
    if value.startswith("["):
        value = value[1 : value.find("]")] if "]" in value else value[1:]
    elif value.count(":") == 1:
        value = value.split(":", 1)[0]
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return ip.ipv4_mapped
    return ip


def resolve_client_ip(
    peer: str | None,
    headers: Mapping[str, str],
    trusted: TrustedProxies,
) -> IPAddress | None:
    """Find the real client address without letting clients spoof it.

    Forwarding headers are only read when the direct peer is a trusted proxy, and
    X-Forwarded-For is walked right to left, stopping at the first untrusted hop:
    anything to the left of it was written by the client.
    """
    peer_ip = parse_ip(peer)
    if peer_ip not in trusted:
        return peer_ip

    for header in CDN_CLIENT_IP_HEADERS:
        ip = parse_ip(headers.get(header))
        if ip is not None:
            return ip

    last = peer_ip
    forwarded = headers.get("x-forwarded-for")
    if forwarded:
        for hop in reversed(forwarded.split(",")):
            ip = parse_ip(hop)
            if ip is None:
                break
            last = ip
            if ip not in trusted:
                return ip
    return last


def ip_bucket(ip: IPAddress, ipv6_prefix: int) -> str:
    if isinstance(ip, ipaddress.IPv6Address) and ipv6_prefix < 128:
        return str(ipaddress.IPv6Network((ip, ipv6_prefix), strict=False))
    return str(ip)


def hash_api_key(api_key: str) -> str:
    # Never store raw secrets in Redis key names; this also keeps "{" and "}"
    # out of the key, which would break the cluster hash tag.
    return hashlib.blake2b(api_key.encode(), digest_size=12).hexdigest()


def identify(
    peer: str | None,
    headers: Mapping[str, str],
    trusted: TrustedProxies,
    ipv6_prefix: int = 64,
) -> ClientIdentity:
    ip = resolve_client_ip(peer, headers, trusted)
    ip_str = str(ip) if ip is not None else None
    ip_key = f"ip:{ip_bucket(ip, ipv6_prefix)}" if ip is not None else "ip:unknown"

    api_key = headers.get(API_KEY_HEADER)
    if api_key:
        return ClientIdentity(
            key=f"k:{hash_api_key(api_key)}", ip_key=ip_key, ip=ip_str, kind="api_key"
        )
    return ClientIdentity(key=ip_key, ip_key=ip_key, ip=ip_str, kind="ip")
