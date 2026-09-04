"""Shared retrying HTTP client used by both the Graph and ServiceNow clients.

Both APIs throttle with 429 + `Retry-After`, so honouring that header is the
difference between a sync that finishes and one that gets progressively
rate-limited into failure.
"""

from __future__ import annotations

import email.utils
import logging
import random
import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from .errors import RetryableHTTPError

log = logging.getLogger(__name__)

RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})

# Never sleep longer than this for a single attempt, even if the server asks.
MAX_SINGLE_BACKOFF_SECONDS = 120.0


def parse_retry_after(value: str | None) -> float | None:
    """Parse a `Retry-After` header, which may be seconds or an HTTP date."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        # A malformed Retry-After must not take the retry path down; fall back
        # to our own exponential backoff instead.
        return None
    if parsed is None:
        return None
    delta = parsed.timestamp() - time.time()
    return max(0.0, delta)


def parse_rate_limit_reset(value: str | None, *, now: float | None = None) -> float | None:
    """Parse ServiceNow's `X-RateLimit-Reset`.

    ServiceNow sends this on a 429 from an inbound REST rate-limit rule, and
    does not always send `Retry-After` alongside it. The value is documented as
    seconds until the window resets, but some releases send an absolute epoch
    instead, so treat anything implausibly large as a timestamp.
    """
    if not value:
        return None
    try:
        seconds = float(value.strip())
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    # Anything past this is a date, not a duration.
    if seconds > 1_000_000_000:
        seconds -= (time.time() if now is None else now)
    return max(0.0, seconds)


def backoff_seconds(attempt: int, retry_after: float | None, base: float = 1.0) -> float:
    """Server hint wins; otherwise exponential backoff with full jitter.

    `attempt` is 1-based.
    """
    if retry_after is not None:
        return min(retry_after, MAX_SINGLE_BACKOFF_SECONDS)
    ceiling = min(base * (2 ** (attempt - 1)), MAX_SINGLE_BACKOFF_SECONDS)
    return random.uniform(0.0, ceiling)


class RetryingClient:
    """Thin wrapper over `httpx.Client` adding auth injection and retries."""

    def __init__(
        self,
        *,
        base_url: str,
        token_provider: Callable[[], str] | None = None,
        auth: httpx.Auth | tuple[str, str] | None = None,
        timeout: float = 60.0,
        max_retries: int = 5,
        default_headers: Mapping[str, str] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._token_provider = token_provider
        self._max_retries = max_retries
        self._sleep = sleep
        headers = {"Accept": "application/json", **(default_headers or {})}
        self._client = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            headers=headers,
            auth=auth,
            follow_redirects=False,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> RetryingClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        headers: Mapping[str, str] | None = None,
        expected: frozenset[int] | set[int] | None = None,
    ) -> httpx.Response:
        """Issue a request, retrying throttles and transient server errors.

        Returns the response whenever the status is in `expected` (default: any
        2xx). Raises `RetryableHTTPError` if retries are exhausted, and returns
        the response untouched for non-retryable non-expected statuses so the
        caller can build a domain-specific error message.
        """
        request_headers = dict(headers or {})
        if self._token_provider is not None:
            request_headers["Authorization"] = f"Bearer {self._token_provider()}"

        last_error: RetryableHTTPError | None = None

        for attempt in range(1, self._max_retries + 2):
            try:
                response = self._client.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers=request_headers,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = RetryableHTTPError(f"{method} {url} failed: {exc}", status_code=0)
                if attempt > self._max_retries:
                    break
                delay = backoff_seconds(attempt, None)
                log.warning(
                    "transport error, retrying",
                    extra={"method": method, "url": url, "attempt": attempt,
                           "delay_s": round(delay, 2), "error": str(exc)},
                )
                self._sleep(delay)
                continue

            if response.status_code in (expected or set()) or (
                expected is None and response.is_success
            ):
                return response

            if response.status_code in RETRYABLE_STATUS:
                retry_after = parse_retry_after(response.headers.get("Retry-After"))
                if retry_after is None:
                    retry_after = parse_rate_limit_reset(
                        response.headers.get("X-RateLimit-Reset")
                    )
                last_error = RetryableHTTPError(
                    f"{method} {url} -> {response.status_code}: {_snippet(response)}",
                    status_code=response.status_code,
                    retry_after=retry_after,
                )
                if attempt > self._max_retries:
                    break
                delay = backoff_seconds(attempt, retry_after)
                log.warning(
                    "throttled or transient error, retrying",
                    extra={"method": method, "url": url, "status": response.status_code,
                           "attempt": attempt, "delay_s": round(delay, 2),
                           "retry_after": retry_after,
                           # Surfacing the remaining budget is what makes a real
                           # instance's actual limits discoverable from the logs.
                           "rate_limit_remaining":
                               response.headers.get("X-RateLimit-Remaining"),
                           "rate_limit_limit": response.headers.get("X-RateLimit-Limit")},
                )
                self._sleep(delay)
                continue

            # Non-retryable: hand back to the caller.
            return response

        assert last_error is not None
        raise last_error


def _snippet(response: httpx.Response, limit: int = 400) -> str:
    try:
        text = response.text
    except Exception:  # pragma: no cover - body already consumed/streamed
        return "<unreadable body>"
    text = " ".join(text.split())
    return text[:limit] + ("..." if len(text) > limit else "")


def describe_error(response: httpx.Response) -> str:
    """Build a compact, log-safe description of a failed response.

    Leads with the method and path. A ServiceNow 403 body says only "User Not
    Authorized" and the caller's own wording ("write failed", "query failed")
    rarely names a URL, so without this an operator cannot tell which of the
    several endpoints a run touches was the one refused -- and the fix, a REST
    API Auth Scope, is bound per API and per method.

    `X-Is-Logged-In: true` on a 4xx is included because it is the tell that
    ServiceNow authenticated the request and then declined the API itself,
    which separates an OAuth scope problem from a credential problem.
    """
    where = _request_label(response)
    logged_in = response.headers.get("X-Is-Logged-In")
    context = f" [X-Is-Logged-In: {logged_in}]" if logged_in else ""
    return (
        f"{where}HTTP {response.status_code} {response.reason_phrase}"
        f"{context}: {_snippet(response)}"
    )


def _request_label(response: httpx.Response) -> str:
    """`METHOD /path -> ` when the originating request is still attached."""
    try:
        request = response.request
    except RuntimeError:  # a Response built without one, e.g. in a unit test
        return ""
    return f"{request.method} {request.url.path} -> "
