import re
from collections.abc import Mapping
from dataclasses import dataclass

# Checked in order; the first header carrying a valid code wins.
COUNTRY_HEADERS = (
    "cf-ipcountry",  # Cloudflare
    "cloudfront-viewer-country",  # AWS CloudFront
    "fastly-geo-country-code",  # Fastly (custom VCL convention)
    "x-vercel-ip-country",  # Vercel
    "x-country-code",  # generic / self-hosted GeoIP
)
REGION_HEADERS = (
    "cloudfront-viewer-country-region",
    "x-vercel-ip-country-region",
    "x-region-code",
)

# ISO 3166-1 alpha-2, plus Cloudflare's T1 (Tor) pseudo-country.
_COUNTRY_RE = re.compile(r"^(?:[A-Z]{2}|T1)$")
_REGION_RE = re.compile(r"^[A-Z0-9]{1,3}$")
# Codes CDNs send when they could not locate the client.
_UNKNOWN = frozenset({"XX", "ZZ"})


@dataclass(frozen=True, slots=True)
class GeoContext:
    country: str | None = None
    region: str | None = None
    source: str | None = None


UNKNOWN = GeoContext()


def parse_geo(headers: Mapping[str, str]) -> GeoContext:
    """Read the client's location from CDN headers.

    Only call this for requests from a trusted proxy, otherwise any client can pick
    its own compliance zone.
    """
    for header in COUNTRY_HEADERS:
        raw = headers.get(header)
        if not raw:
            continue
        country = raw.strip().upper()
        if not _COUNTRY_RE.match(country) or country in _UNKNOWN:
            continue
        return GeoContext(country=country, region=_parse_region(headers), source=header)
    return UNKNOWN


def _parse_region(headers: Mapping[str, str]) -> str | None:
    for header in REGION_HEADERS:
        raw = headers.get(header)
        if raw and _REGION_RE.match(region := raw.strip().upper()):
            return region
    return None
