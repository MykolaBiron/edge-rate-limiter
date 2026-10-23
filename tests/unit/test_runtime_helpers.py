import pytest

from gateway.config import Settings
from gateway.limiters import Decision
from gateway.limiters.engine import SCRIPT
from gateway.proxy import _strip
from gateway.redis_client import CircuitBreaker
from gateway.service import rate_limit_headers


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _breaker(clock, **kw):
    return CircuitBreaker(
        **{
            "min_failures": 3,
            "failure_ratio": 0.5,
            "window_s": 1,
            "cooldown_s": 1,
            "clock": clock,
            **kw,
        }
    )


def test_breaker_stays_closed_below_min_failures():
    breaker = _breaker(Clock())
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.allow() and not breaker.is_open


def test_breaker_opens_at_min_failures_and_closes_after_cooldown():
    clock = Clock()
    breaker = _breaker(clock)
    for _ in range(3):
        breaker.record_failure()
    assert breaker.is_open and not breaker.allow()
    clock.t = 0.999
    assert not breaker.allow()
    clock.t = 1.0
    assert breaker.allow() and not breaker.is_open


def test_occasional_failures_among_successes_do_not_trip():
    """The case that matters under load: a few timeouts during a latency blip."""
    breaker = _breaker(Clock(), min_failures=5)
    for _ in range(100):
        breaker.record_success()
    for _ in range(40):
        breaker.record_failure()
    assert not breaker.is_open


def test_ratio_is_required_as_well_as_count():
    breaker = _breaker(Clock(), failure_ratio=0.5)
    for _ in range(4):
        breaker.record_success()
    for _ in range(3):
        breaker.record_failure()
    assert not breaker.is_open  # 3 of 7
    breaker.record_failure()
    assert breaker.is_open  # 4 of 8


def test_counts_reset_each_window():
    clock = Clock()
    breaker = _breaker(clock)
    breaker.record_failure()
    breaker.record_failure()
    clock.t = 1.0
    breaker.record_failure()
    assert not breaker.is_open


def test_failures_after_cooldown_must_add_up_again():
    clock = Clock()
    breaker = _breaker(clock)
    for _ in range(3):
        breaker.record_failure()
    clock.t = 5
    assert breaker.allow()
    breaker.record_failure()
    assert not breaker.is_open
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.is_open


def _decision(**kw):
    base = dict(allowed=True, limit=10, remaining=4, retry_after_ms=0, reset_ms=1500)
    return Decision(**{**base, **kw})


def test_headers_for_allowed_request():
    headers = rate_limit_headers(_decision(), "api")
    assert headers == {
        "x-ratelimit-limit": "10",
        "x-ratelimit-remaining": "4",
        "x-ratelimit-reset": "2",
        "x-ratelimit-policy": "api",
    }


@pytest.mark.parametrize(
    ("retry_ms", "seconds"), [(1, "1"), (999, "1"), (1000, "1"), (1001, "2"), (0, "1")]
)
def test_retry_after_rounds_up_to_whole_seconds(retry_ms, seconds):
    headers = rate_limit_headers(_decision(allowed=False, retry_after_ms=retry_ms), "api")
    assert headers["retry-after"] == seconds


def test_negative_remaining_is_clamped():
    assert rate_limit_headers(_decision(remaining=-3), "p")["x-ratelimit-remaining"] == "0"


def test_strip_removes_hop_by_hop_and_connection_listed_headers():
    headers = [
        (b"Connection", b"keep-alive, X-Secret-Hop"),
        (b"Keep-Alive", b"timeout=5"),
        (b"Transfer-Encoding", b"chunked"),
        (b"X-Secret-Hop", b"1"),
        (b"Upgrade", b"websocket"),
        (b"Content-Type", b"text/plain"),
        (b"Set-Cookie", b"a=1"),
        (b"Set-Cookie", b"b=2"),
    ]
    assert _strip(headers) == [
        (b"Content-Type", b"text/plain"),
        (b"Set-Cookie", b"a=1"),
        (b"Set-Cookie", b"b=2"),
    ]


def test_strip_extra_names():
    assert _strip([(b"Host", b"x"), (b"Accept", b"*/*")], frozenset({"host"})) == [
        (b"Accept", b"*/*")
    ]


def test_settings_parse_trusted_proxies_from_env(monkeypatch):
    monkeypatch.setenv("GW_TRUSTED_PROXIES", "10.0.0.0/8, 172.16.0.0/12,,")
    monkeypatch.setenv("GW_FAIL_MODE", "closed")
    settings = Settings(_env_file=None)
    assert settings.trusted_proxies == ["10.0.0.0/8", "172.16.0.0/12"]
    assert settings.fail_mode == "closed"


def test_settings_reject_bad_fail_mode(monkeypatch):
    monkeypatch.setenv("GW_FAIL_MODE", "sometimes")
    with pytest.raises(ValueError):
        Settings(_env_file=None)


def test_script_bundles_every_algorithm():
    for name in ("token_bucket", "leaky_bucket", "fixed_window", "sliding_window", "sliding_log"):
        assert f"algos.{name} = function" in SCRIPT
    assert SCRIPT.index("local algos = {}") < SCRIPT.index("algos.token_bucket")
