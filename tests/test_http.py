from __future__ import annotations

import email.utils
import time

import httpx
import pytest
import respx

from intune_cmdb_sync.errors import RetryableHTTPError
from intune_cmdb_sync.http import (
    MAX_SINGLE_BACKOFF_SECONDS,
    RetryingClient,
    backoff_seconds,
    describe_error,
    parse_rate_limit_reset,
    parse_retry_after,
)

BASE = "https://api.example.com"


class TestParseRetryAfter:
    def test_seconds_form(self):
        assert parse_retry_after("30") == 30.0

    def test_http_date_form(self):
        future = email.utils.formatdate(time.time() + 45, usegmt=True)
        value = parse_retry_after(future)
        assert value is not None and 30 <= value <= 60

    def test_past_date_clamps_to_zero(self):
        past = email.utils.formatdate(time.time() - 500, usegmt=True)
        assert parse_retry_after(past) == 0.0

    @pytest.mark.parametrize("raw", [None, "", "   "])
    def test_absent_header(self, raw):
        assert parse_retry_after(raw) is None


class TestBackoff:
    def test_server_hint_wins(self):
        assert backoff_seconds(1, 12.0) == 12.0

    def test_server_hint_is_capped(self):
        assert backoff_seconds(1, 9999.0) == MAX_SINGLE_BACKOFF_SECONDS

    def test_jittered_growth_stays_within_ceiling(self):
        for attempt in range(1, 8):
            ceiling = min(2 ** (attempt - 1), MAX_SINGLE_BACKOFF_SECONDS)
            assert 0.0 <= backoff_seconds(attempt, None) <= ceiling


class TestRetryingClient:
    def _client(self, slept: list[float], **kwargs) -> RetryingClient:
        return RetryingClient(base_url=BASE, sleep=slept.append, **kwargs)

    @respx.mock
    def test_success_first_try(self):
        respx.get(f"{BASE}/thing").mock(return_value=httpx.Response(200, json={"ok": True}))
        slept: list[float] = []
        with self._client(slept) as client:
            response = client.request("GET", "/thing")
        assert response.json() == {"ok": True}
        assert slept == []

    @respx.mock
    def test_retries_429_and_honours_retry_after(self):
        route = respx.get(f"{BASE}/thing").mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "7"}, text="slow down"),
                httpx.Response(200, json={"ok": True}),
            ]
        )
        slept: list[float] = []
        with self._client(slept) as client:
            response = client.request("GET", "/thing")
        assert response.status_code == 200
        assert route.call_count == 2
        assert slept == [7.0]

    @respx.mock
    def test_gives_up_after_max_retries(self):
        respx.get(f"{BASE}/thing").mock(
            return_value=httpx.Response(503, headers={"Retry-After": "1"})
        )
        slept: list[float] = []
        with self._client(slept, max_retries=2) as client, pytest.raises(RetryableHTTPError) as exc:
            client.request("GET", "/thing")
        assert exc.value.status_code == 503
        assert len(slept) == 2

    @respx.mock
    def test_non_retryable_status_is_returned_not_raised(self):
        respx.get(f"{BASE}/thing").mock(return_value=httpx.Response(400, text="bad input"))
        slept: list[float] = []
        with self._client(slept) as client:
            response = client.request("GET", "/thing")
        assert response.status_code == 400
        assert slept == []

    @respx.mock
    def test_transport_errors_are_retried(self):
        respx.get(f"{BASE}/thing").mock(
            side_effect=[httpx.ConnectError("boom"), httpx.Response(200, json={})]
        )
        slept: list[float] = []
        with self._client(slept) as client:
            assert client.request("GET", "/thing").status_code == 200
        assert len(slept) == 1

    @respx.mock
    def test_bearer_token_is_attached_per_request(self):
        tokens = iter(["first", "second"])
        route = respx.get(f"{BASE}/thing").mock(return_value=httpx.Response(200, json={}))
        slept: list[float] = []
        with self._client(slept, token_provider=lambda: next(tokens)) as client:
            client.request("GET", "/thing")
            client.request("GET", "/thing")
        assert route.calls[0].request.headers["Authorization"] == "Bearer first"
        assert route.calls[1].request.headers["Authorization"] == "Bearer second"

    @respx.mock
    def test_expected_statuses_short_circuit_retries(self):
        respx.get(f"{BASE}/thing").mock(return_value=httpx.Response(429))
        slept: list[float] = []
        with self._client(slept) as client:
            response = client.request("GET", "/thing", expected={429})
        assert response.status_code == 429
        assert slept == []


def test_describe_error_collapses_whitespace_and_truncates():
    response = httpx.Response(500, text="a\n\n   b" + "c" * 1000)
    described = describe_error(response)
    assert described.startswith("HTTP 500")
    assert "a b" in described
    assert described.endswith("...")


class TestServiceNowRateLimitHeaders:
    """ServiceNow answers a tripped inbound REST rate-limit rule with 429 and
    `X-RateLimit-Reset`, and does not always send `Retry-After` alongside it."""

    def test_reset_parsed_as_a_duration(self):
        assert parse_rate_limit_reset("45") == 45.0

    def test_reset_parsed_as_an_absolute_epoch(self):
        now = 1_800_000_000.0
        assert parse_rate_limit_reset(str(now + 30), now=now) == pytest.approx(30.0)

    def test_junk_and_expired_values_fall_back_to_our_own_backoff(self):
        assert parse_rate_limit_reset(None) is None
        assert parse_rate_limit_reset("soon") is None
        assert parse_rate_limit_reset("0") is None

    @respx.mock
    def test_reset_header_is_honoured_when_retry_after_is_absent(self):
        slept: list[float] = []
        route = respx.get("https://acme.service-now.com/x")
        route.side_effect = [
            httpx.Response(429, headers={"X-RateLimit-Reset": "7"}, json={}),
            httpx.Response(200, json={"ok": True}),
        ]
        client = RetryingClient(
            base_url="https://acme.service-now.com",
            max_retries=2,
            sleep=slept.append,
        )
        assert client.request("GET", "/x").status_code == 200
        assert slept == [7.0]
