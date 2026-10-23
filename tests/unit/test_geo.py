import pytest

from gateway.geo import COUNTRY_HEADERS, UNKNOWN, parse_geo


@pytest.mark.parametrize("header", COUNTRY_HEADERS)
def test_reads_every_cdn_country_header(header):
    geo = parse_geo({header: "DE"})
    assert geo.country == "DE"
    assert geo.source == header


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("de", "DE"), (" fr ", "FR"), ("T1", "T1"), ("t1", "T1"), ("US", "US")],
)
def test_normalises_country_codes(raw, expected):
    assert parse_geo({"cf-ipcountry": raw}).country == expected


@pytest.mark.parametrize("raw", ["XX", "ZZ", "", "DEU", "D", "1A", "D-", "<script>", "  "])
def test_rejects_unknown_or_malformed_codes(raw):
    assert parse_geo({"cf-ipcountry": raw}) == UNKNOWN


def test_falls_through_to_next_header_when_first_is_invalid():
    geo = parse_geo({"cf-ipcountry": "XX", "cloudfront-viewer-country": "JP"})
    assert geo.country == "JP"
    assert geo.source == "cloudfront-viewer-country"


def test_header_priority():
    geo = parse_geo({"x-country-code": "BR", "cf-ipcountry": "AR"})
    assert geo.country == "AR"


def test_no_headers():
    assert parse_geo({}) == UNKNOWN


@pytest.mark.parametrize(
    ("headers", "region"),
    [
        ({"cloudfront-viewer-country": "US", "cloudfront-viewer-country-region": "CA"}, "CA"),
        ({"x-vercel-ip-country": "US", "x-vercel-ip-country-region": "tx"}, "TX"),
        ({"x-country-code": "DE", "x-region-code": "BY"}, "BY"),
        ({"cf-ipcountry": "US", "x-region-code": "TOO-LONG"}, None),
        ({"cf-ipcountry": "US"}, None),
    ],
)
def test_region(headers, region):
    assert parse_geo(headers).region == region


def test_region_without_country_is_ignored():
    assert parse_geo({"cloudfront-viewer-country-region": "CA"}) == UNKNOWN
