from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

import httpx
import pytest

from scholarly_retrieval.api import create_app
from scholarly_retrieval.distributed import (
    ExpansionJob,
    ExpansionJobPayload,
    JobStatus,
    RedisExpansionJobStore,
    RedisFixedWindowLimiter,
)
from scholarly_retrieval.worker import execute_expansion_job


class FakePipeline:
    def __init__(self, redis: FakeRedis) -> None:
        self.redis = redis
        self.operations: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    async def __aenter__(self) -> FakePipeline:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def set(self, *args: Any, **kwargs: Any) -> FakePipeline:
        self.operations.append(("set", args, kwargs))
        return self

    def rpush(self, *args: Any, **kwargs: Any) -> FakePipeline:
        self.operations.append(("rpush", args, kwargs))
        return self

    async def execute(self) -> None:
        for name, args, kwargs in self.operations:
            await getattr(self.redis, name)(*args, **kwargs)


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.queues: dict[str, deque[str]] = {}
        self.counters: dict[str, int] = {}
        self.closed = False

    def pipeline(self, *, transaction: bool) -> FakePipeline:
        assert transaction is True
        return FakePipeline(self)

    async def set(self, key: str, value: str, **_kwargs: Any) -> None:
        self.values[key] = value

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def rpush(self, key: str, value: str) -> None:
        self.queues.setdefault(key, deque()).append(value)

    async def blpop(
        self,
        key: str,
        *,
        timeout: int,  # noqa: ASYNC109 - mirrors redis-py
    ) -> tuple[str, str] | None:
        assert timeout >= 0
        queue = self.queues.get(key)
        return (key, queue.popleft()) if queue else None

    async def eval(
        self,
        _script: str,
        key_count: int,
        key: str,
        _expiry: int,
    ) -> list[int]:
        assert key_count == 1
        self.counters[key] = self.counters.get(key, 0) + 1
        return [self.counters[key], 60]

    async def aclose(self) -> None:
        self.closed = True


def test_redis_job_store_round_trip_claim_and_cross_replica_cancel() -> None:
    async def scenario() -> None:
        redis = FakeRedis()
        store = RedisExpansionJobStore(redis, prefix="test", ttl_seconds=60)
        job = ExpansionJob(job_id="job-1", status=JobStatus.QUEUED)
        payload = ExpansionJobPayload(
            request={"identifier": "10.1234/seed", "depth": 1},
            sources=["openalex"],
        )
        await store.enqueue(job, payload)

        assert await store.dequeue(timeout_seconds=0) == "job-1"
        claimed = await store.mark_running("job-1")
        assert claimed is not None and claimed.status == JobStatus.RUNNING
        assert (await store.payload("job-1")) == payload

        cancelled = await store.request_cancel("job-1")
        assert cancelled is not None and cancelled.status == JobStatus.CANCEL_REQUESTED
        assert await store.cancellation_requested("job-1") is True
        assert (await store.get("job-1")).status == JobStatus.CANCEL_REQUESTED
        await store.close()
        assert redis.closed is True

    asyncio.run(scenario())


def test_redis_limiter_shares_quota_without_storing_raw_subject() -> None:
    async def scenario() -> None:
        redis = FakeRedis()
        limiter = RedisFixedWindowLimiter(redis, 2, prefix="test")
        assert await limiter.admit("secret-api-key") == (True, 0)
        assert await limiter.admit("secret-api-key") == (True, 0)
        admitted, retry_after = await limiter.admit("secret-api-key")
        assert admitted is False and retry_after == 60
        assert all("secret-api-key" not in key for key in redis.counters)

    asyncio.run(scenario())


class FailingExpansionService:
    async def expand(self, *_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("provider fixture failed")


def test_worker_records_stable_failure_category() -> None:
    async def scenario() -> None:
        store = RedisExpansionJobStore(FakeRedis(), prefix="test", ttl_seconds=60)
        await store.enqueue(
            ExpansionJob(job_id="job-failed", status=JobStatus.QUEUED),
            ExpansionJobPayload(request={"identifier": "seed", "depth": 1}),
        )
        job_id = await store.dequeue(timeout_seconds=0)
        assert job_id is not None
        await execute_expansion_job(
            job_id,
            store=store,
            service=FailingExpansionService(),  # type: ignore[arg-type]
            cancel_poll_seconds=0,
        )
        result = await store.get(job_id)
        assert result is not None
        assert result.status == JobStatus.FAILED
        assert result.error_code == "RuntimeError"

    asyncio.run(scenario())


class IdleService:
    providers: dict[str, Any] = {}

    async def close(self) -> None:
        return None


def test_api_uses_shared_job_store_when_redis_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        job_store = RedisExpansionJobStore(FakeRedis(), prefix="test", ttl_seconds=60)
        limiter = RedisFixedWindowLimiter(FakeRedis(), 10, prefix="test")
        monkeypatch.setenv("SCHOLAR_REDIS_URL", "redis://fixture")
        monkeypatch.setattr(
            RedisExpansionJobStore,
            "from_url",
            classmethod(lambda _cls, *_args, **_kwargs: job_store),
        )
        monkeypatch.setattr(
            RedisFixedWindowLimiter,
            "from_url",
            classmethod(lambda _cls, *_args, **_kwargs: limiter),
        )
        app = create_app(  # type: ignore[arg-type]
            lambda: IdleService(),
            api_keys=set(),
            rate_limit_per_minute=10,
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/v1/jobs/expand",
                    json={"identifier": "seed", "depth": 1},
                )
                assert response.status_code == 202
                job_id = response.json()["job_id"]
                assert (await client.get(f"/v1/jobs/{job_id}")).json()["status"] == "queued"
                cancelled = await client.delete(f"/v1/jobs/{job_id}")
                assert cancelled.json()["status"] == "cancelled"

    asyncio.run(scenario())
