from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from scholarly_retrieval.reliability import (
    CircuitOpenError,
    ReliableHttpClient,
    RetryPolicy,
    parse_retry_after,
    redact_url,
)
from scholarly_retrieval.storage import SQLiteStore


def test_retry_after_supports_seconds_and_http_date() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert parse_retry_after("2.5", now=now) == 2.5
    assert parse_retry_after(format_datetime(now + timedelta(seconds=9)), now=now) == 9
    assert parse_retry_after("invalid", now=now) is None


def test_url_redaction_removes_query_credentials() -> None:
    redacted = redact_url(
        "https://api.example/works?query=graph&api_key=top-secret&token=second-secret"
    )
    assert "top-secret" not in redacted
    assert "second-secret" not in redacted
    assert "query=graph" in redacted


def test_retry_is_bounded_and_success_is_cached() -> None:
    async def scenario() -> None:
        calls = 0
        sleeps: list[float] = []

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(503, headers={"Retry-After": "0"}, request=request)
            return httpx.Response(
                200,
                json={"ok": True},
                headers={"ETag": "test", "Authorization": "must-not-persist"},
                request=request,
            )

        async def fake_sleep(delay: float) -> None:
            sleeps.append(delay)

        store = SQLiteStore(":memory:")
        client = httpx.AsyncClient(
            base_url="https://example.test", transport=httpx.MockTransport(handler)
        )
        reliable = ReliableHttpClient(
            client,
            provider="fixture",
            store=store,
            policy=RetryPolicy(max_attempts=3, jitter_ratio=0),
            sleep=fake_sleep,
        )

        params = {"q": "graph", "api_key": "top-secret"}
        first = await reliable.get("/works", params=params, operation="search")
        second = await reliable.get("/works", params=params, operation="search")

        assert first.json() == {"ok": True}
        assert second.json() == {"ok": True}
        assert second.extensions["scholarly_cache"]["hit"] is True
        assert calls == 2
        assert sleeps == [0]
        assert store.stats() == {
            "http_cache": 1,
            "raw_responses": 2,
            "provider_attempts": 2,
            "query_runs": 0,
            "cursor_checkpoints": 0,
            "identity_events": 0,
            "citation_assertions": 0,
            "visible_citation_edges": 0,
            "citation_edge_status_events": 0,
        }
        row = store._connection.execute("SELECT headers_json, url FROM http_cache").fetchone()
        assert "authorization" not in row[0].lower()
        assert "top-secret" not in row[1]
        assert "REDACTED" in row[1]

        await client.aclose()
        store.close()

    asyncio.run(scenario())


def test_retryable_status_stops_at_max_attempts() -> None:
    async def scenario() -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(429, headers={"Retry-After": "0"}, request=request)

        async def no_sleep(delay: float) -> None:
            return None

        client = httpx.AsyncClient(
            base_url="https://example.test", transport=httpx.MockTransport(handler)
        )
        reliable = ReliableHttpClient(
            client,
            provider="fixture",
            policy=RetryPolicy(max_attempts=3, jitter_ratio=0),
            sleep=no_sleep,
        )
        response = await reliable.get("/works")

        assert response.status_code == 429
        assert calls == 3
        assert response.extensions["scholarly_reliability"] == {
            "attempt_count": 3,
            "retry_delays": [0, 0],
        }
        await client.aclose()

    asyncio.run(scenario())


def test_retry_after_is_not_shortened_to_exponential_backoff_ceiling() -> None:
    request = httpx.Request("GET", "https://example.test/works")
    response = httpx.Response(429, headers={"Retry-After": "45"}, request=request)
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response))
    reliable = ReliableHttpClient(
        client,
        provider="fixture",
        policy=RetryPolicy(max_delay_seconds=10, max_retry_after_seconds=60),
    )

    assert reliable._retry_delay(response, attempt=1) == 45


def test_retry_after_still_has_a_safety_ceiling() -> None:
    request = httpx.Request("GET", "https://example.test/works")
    response = httpx.Response(429, headers={"Retry-After": "3600"}, request=request)
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response))
    reliable = ReliableHttpClient(
        client,
        provider="fixture",
        policy=RetryPolicy(max_retry_after_seconds=120),
    )

    assert reliable._retry_delay(response, attempt=1) == 120


def test_missing_retry_after_uses_exponential_backoff() -> None:
    async def scenario() -> None:
        calls = 0
        sleeps: list[float] = []

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200 if calls == 3 else 429, request=request)

        async def fake_sleep(delay: float) -> None:
            sleeps.append(delay)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        reliable = ReliableHttpClient(
            client,
            provider="fixture",
            policy=RetryPolicy(
                max_attempts=3,
                base_delay_seconds=2,
                max_delay_seconds=30,
                jitter_ratio=0,
            ),
            sleep=fake_sleep,
        )

        response = await reliable.get("https://example.test/works")

        assert sleeps == [2, 4]
        assert response.extensions["scholarly_reliability"] == {
            "attempt_count": 3,
            "retry_delays": [2, 4],
        }
        await client.aclose()

    asyncio.run(scenario())


def test_cache_key_is_independent_of_parameter_order() -> None:
    async def scenario() -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={"ok": True}, request=request)

        store = SQLiteStore(":memory:")
        client = httpx.AsyncClient(
            base_url="https://example.test", transport=httpx.MockTransport(handler)
        )
        reliable = ReliableHttpClient(client, provider="fixture", store=store)
        await reliable.get("/works", params={"b": "2", "a": "1"})
        cached = await reliable.get("/works", params={"a": "1", "b": "2"})

        assert calls == 1
        assert cached.extensions["scholarly_cache"]["hit"] is True
        await client.aclose()
        store.close()

    asyncio.run(scenario())


def test_transport_errors_are_bounded_and_preserve_error_type() -> None:
    async def scenario() -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            raise httpx.ConnectError("offline", request=request)

        async def no_sleep(delay: float) -> None:
            return None

        client = httpx.AsyncClient(
            base_url="https://example.test", transport=httpx.MockTransport(handler)
        )
        reliable = ReliableHttpClient(
            client,
            provider="fixture",
            policy=RetryPolicy(max_attempts=2, jitter_ratio=0),
            sleep=no_sleep,
        )
        with pytest.raises(httpx.ConnectError, match="offline"):
            await reliable.get("/works")
        assert calls == 2
        await client.aclose()

    asyncio.run(scenario())


def test_circuit_opens_after_repeated_retryable_failures() -> None:
    async def scenario() -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(503, request=request)

        client = httpx.AsyncClient(
            base_url="https://example.test", transport=httpx.MockTransport(handler)
        )
        reliable = ReliableHttpClient(
            client,
            provider="fixture",
            policy=RetryPolicy(
                max_attempts=1,
                circuit_failure_threshold=2,
                circuit_cooldown_seconds=60,
            ),
        )
        assert (await reliable.get("/works")).status_code == 503
        assert (await reliable.get("/works")).status_code == 503
        with pytest.raises(CircuitOpenError, match="circuit open"):
            await reliable.get("/works")
        assert calls == 2
        await client.aclose()

    asyncio.run(scenario())
