"""Bounded retry, caching, pacing, and circuit-breaking for provider GET requests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from time import monotonic
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from .storage import SQLiteStore

Sleep = Callable[[float], Awaitable[None]]
current_run_id: ContextVar[str | None] = ContextVar("scholarly_run_id", default=None)
SENSITIVE_QUERY_NAMES = {"api_key", "apikey", "key", "token", "access_token"}


class CircuitOpenError(httpx.RequestError):
    """Raised before I/O when repeated upstream failures opened the circuit."""


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 10.0
    jitter_ratio: float = 0.2
    cache_ttl_seconds: float = 3600.0
    min_interval_seconds: float = 0.0
    max_concurrency: int = 2
    circuit_failure_threshold: int = 5
    circuit_cooldown_seconds: float = 30.0
    retry_statuses: frozenset[int] = frozenset({429, 500, 502, 503, 504})

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")


class ReliableHttpClient:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        provider: str,
        store: SQLiteStore | None = None,
        policy: RetryPolicy | None = None,
        sleep: Sleep = asyncio.sleep,
        random_value: Callable[[], float] = random.random,
    ) -> None:
        self.client = client
        self.provider = provider
        self.store = store
        self.policy = policy or RetryPolicy()
        self._sleep = sleep
        self._random_value = random_value
        self._semaphore = asyncio.Semaphore(self.policy.max_concurrency)
        self._pace_lock = asyncio.Lock()
        self._last_request_started = 0.0
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0

    async def get(
        self,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        operation: str = "get",
        use_cache: bool = True,
    ) -> httpx.Response:
        # Sorting gives semantically identical GETs one deterministic cache key.
        # The real URL is hashed; the separately persisted URL is redacted.
        sorted_params = sorted((key, str(value)) for key, value in (params or {}).items())
        request = self.client.build_request("GET", path, params=sorted_params)
        cache_key = make_cache_key(self.provider, "GET", str(request.url))
        if use_cache and self.store:
            cached = self.store.get_cached_response(cache_key)
            if cached:
                return httpx.Response(
                    cached.status_code,
                    headers=cached.headers,
                    content=cached.body,
                    request=request,
                    extensions={"scholarly_cache": {"hit": True}},
                )
        if monotonic() < self._circuit_open_until:
            raise CircuitOpenError(f"circuit open for provider {self.provider}", request=request)

        async with self._semaphore:
            return await self._attempts(
                "GET",
                path,
                sorted_params,
                operation=operation,
                cache_key=cache_key,
                use_cache=use_cache,
            )

    async def post_json(
        self,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json_body: Mapping[str, object],
        operation: str,
        use_cache: bool = True,
    ) -> httpx.Response:
        """POST a side-effect-free provider query with body-aware caching.

        This is intentionally separate from a generic mutation helper: callers
        may use it only for documented read/query endpoints such as scholarly
        recommendation APIs, where replay is safe.
        """

        sorted_params = sorted((key, str(value)) for key, value in (params or {}).items())
        canonical_body = json.dumps(
            json_body, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        request = self.client.build_request(
            "POST", path, params=sorted_params, content=canonical_body.encode()
        )
        cache_key = make_cache_key(self.provider, "POST", f"{request.url}\n{canonical_body}")
        if use_cache and self.store:
            cached = self.store.get_cached_response(cache_key)
            if cached:
                return httpx.Response(
                    cached.status_code,
                    headers=cached.headers,
                    content=cached.body,
                    request=request,
                    extensions={"scholarly_cache": {"hit": True}},
                )
        if monotonic() < self._circuit_open_until:
            raise CircuitOpenError(f"circuit open for provider {self.provider}", request=request)
        async with self._semaphore:
            return await self._attempts(
                "POST",
                path,
                sorted_params,
                content=canonical_body.encode(),
                operation=operation,
                cache_key=cache_key,
                use_cache=use_cache,
            )

    async def _attempts(
        self,
        method: str,
        path: str,
        params: list[tuple[str, str]],
        *,
        content: bytes | None = None,
        operation: str,
        cache_key: str,
        use_cache: bool,
    ) -> httpx.Response:
        last_error: httpx.RequestError | None = None
        for attempt in range(1, self.policy.max_attempts + 1):
            # POST is admitted only through post_json's side-effect-free query
            # contract. State-changing provider operations must not use this loop.
            await self._pace()
            request = self.client.build_request(
                method,
                path,
                params=params,
                content=content,
                headers={"content-type": "application/json"} if content else None,
            )
            try:
                response = await self.client.send(request)
            except httpx.RequestError as exc:
                last_error = exc
                retry = attempt < self.policy.max_attempts
                delay = self._backoff(attempt) if retry else None
                self._record_attempt(
                    operation,
                    cache_key,
                    attempt,
                    outcome="transport_error",
                    error_type=type(exc).__name__,
                    retry_delay=delay,
                )
                self._register_failure()
                if not retry:
                    raise
                await self._sleep(delay or 0.0)
                continue

            retry = (
                response.status_code in self.policy.retry_statuses
                and attempt < self.policy.max_attempts
            )
            delay = self._retry_delay(response, attempt) if retry else None
            self._record_response(response, cache_key, use_cache=use_cache)
            self._record_attempt(
                operation,
                cache_key,
                attempt,
                outcome="retryable_status" if retry else "response",
                status_code=response.status_code,
                retry_delay=delay,
            )
            if response.status_code in self.policy.retry_statuses:
                self._register_failure()
            else:
                self._register_success()
            if not retry:
                return response
            await self._sleep(delay or 0.0)
        if last_error:
            raise last_error
        raise RuntimeError("retry loop ended without a response")

    async def _pace(self) -> None:
        if self.policy.min_interval_seconds <= 0:
            return
        async with self._pace_lock:
            now = monotonic()
            wait_for = self.policy.min_interval_seconds - (now - self._last_request_started)
            if wait_for > 0:
                await self._sleep(wait_for)
            self._last_request_started = monotonic()

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        retry_after = parse_retry_after(response.headers.get("retry-after"))
        return min(
            self.policy.max_delay_seconds,
            retry_after if retry_after is not None else self._backoff(attempt),
        )

    def _backoff(self, attempt: int) -> float:
        base = min(
            self.policy.max_delay_seconds,
            self.policy.base_delay_seconds * (2 ** (attempt - 1)),
        )
        jitter = base * self.policy.jitter_ratio * self._random_value()
        return min(self.policy.max_delay_seconds, base + jitter)

    def _record_response(
        self, response: httpx.Response, cache_key: str, *, use_cache: bool
    ) -> None:
        if not self.store:
            return
        self.store.record_response(
            cache_key=cache_key,
            provider=self.provider,
            method=response.request.method,
            url=redact_url(str(response.request.url)),
            status_code=response.status_code,
            headers=dict(response.headers),
            body=response.content,
            cache_ttl_seconds=self.policy.cache_ttl_seconds if use_cache else None,
        )

    def _record_attempt(
        self,
        operation: str,
        cache_key: str,
        attempt: int,
        *,
        outcome: str,
        status_code: int | None = None,
        error_type: str | None = None,
        retry_delay: float | None = None,
    ) -> None:
        if self.store:
            self.store.record_attempt(
                provider=self.provider,
                operation=operation,
                cache_key=cache_key,
                attempt_number=attempt,
                outcome=outcome,
                status_code=status_code,
                error_type=error_type,
                retry_delay=retry_delay,
                run_id=current_run_id.get(),
            )

    def _register_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.policy.circuit_failure_threshold:
            self._circuit_open_until = monotonic() + self.policy.circuit_cooldown_seconds

    def _register_success(self) -> None:
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0


def make_cache_key(provider: str, method: str, url: str) -> str:
    payload = f"{provider}\n{method.upper()}\n{url}".encode()
    return hashlib.sha256(payload).hexdigest()


def redact_url(url: str) -> str:
    parts = urlsplit(url)
    redacted_query = urlencode(
        [
            (key, "[REDACTED]" if key.casefold() in SENSITIVE_QUERY_NAMES else value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
        ]
    )
    return urlunsplit((parts.scheme, parts.netloc, parts.path, redacted_query, parts.fragment))


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    if not value:
        return None
    stripped = value.strip()
    try:
        return max(0.0, float(stripped))
    except ValueError:
        pass
    try:
        target = parsedate_to_datetime(stripped)
    except (TypeError, ValueError, OverflowError):
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    return max(0.0, (target - current).total_seconds())
