"""Optional Redis coordination for multi-replica API processes and workers."""

from __future__ import annotations

import hashlib
import time
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from .models import GraphExpansionResult


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCEL_REQUESTED = "cancel_requested"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ExpansionJob(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    status: JobStatus
    result: GraphExpansionResult | None = None
    error_code: str | None = None


class ExpansionJobPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: dict[str, Any]
    sources: list[str] | None = None


class RedisExpansionJobStore:
    """Durable queue handles and results shared by independent API replicas.

    Redis is imported lazily so Library/CLI users retain a zero-service default.
    The first vertical slice uses a Redis list: queued handles survive with AOF,
    but a worker crash after BLPOP requires the client to resubmit the request.
    Graph expansion is deterministic, so such resubmission is safe.
    """

    def __init__(
        self,
        redis_client: Any,
        *,
        prefix: str = "scholar",
        ttl_seconds: int = 86_400,
    ) -> None:
        if ttl_seconds < 60:
            raise ValueError("Redis job TTL must be at least 60 seconds")
        self._redis = redis_client
        self._prefix = prefix.rstrip(":")
        self._ttl = ttl_seconds

    @classmethod
    def from_url(
        cls,
        url: str,
        *,
        prefix: str = "scholar",
        ttl_seconds: int = 86_400,
    ) -> RedisExpansionJobStore:
        try:
            from redis.asyncio import Redis
        except ImportError as exc:  # pragma: no cover - depends on optional install
            raise RuntimeError(
                "SCHOLAR_REDIS_URL requires installation with .[distributed]"
            ) from exc
        return cls(
            Redis.from_url(url, decode_responses=True),
            prefix=prefix,
            ttl_seconds=ttl_seconds,
        )

    async def enqueue(self, job: ExpansionJob, payload: ExpansionJobPayload) -> None:
        """Atomically publish state/payload before making the job claimable."""

        async with self._redis.pipeline(transaction=True) as pipeline:
            pipeline.set(self._job_key(job.job_id), job.model_dump_json(), ex=self._ttl)
            pipeline.set(
                self._payload_key(job.job_id),
                payload.model_dump_json(),
                ex=self._ttl,
            )
            pipeline.rpush(self._queue_key, job.job_id)
            await pipeline.execute()

    async def get(self, job_id: str) -> ExpansionJob | None:
        raw = await self._redis.get(self._job_key(job_id))
        return ExpansionJob.model_validate_json(raw) if raw else None

    async def payload(self, job_id: str) -> ExpansionJobPayload | None:
        raw = await self._redis.get(self._payload_key(job_id))
        return ExpansionJobPayload.model_validate_json(raw) if raw else None

    async def dequeue(self, *, timeout_seconds: int = 5) -> str | None:
        item = await self._redis.blpop(self._queue_key, timeout=timeout_seconds)
        if not item:
            return None
        _queue, job_id = item
        return self._text(job_id)

    async def update(self, job: ExpansionJob) -> None:
        await self._redis.set(
            self._job_key(job.job_id),
            job.model_dump_json(),
            ex=self._ttl,
        )

    async def mark_running(self, job_id: str) -> ExpansionJob | None:
        """Claim only a still-queued job; cancelled queue entries are harmless."""

        if await self.cancellation_requested(job_id):
            return None
        job = await self.get(job_id)
        if job is None or job.status != JobStatus.QUEUED:
            return None
        running = job.model_copy(update={"status": JobStatus.RUNNING})
        # BLPOP gives one worker the queue item. This write is therefore the
        # single consumer transition, while API replicas only request cancel.
        await self.update(running)
        return running

    async def request_cancel(self, job_id: str) -> ExpansionJob | None:
        job = await self.get(job_id)
        if job is None:
            return None
        # The separate flag cannot be lost if a worker concurrently publishes
        # RUNNING from a stale queued snapshot. Workers poll this key directly.
        await self._redis.set(self._cancel_key(job_id), "1", ex=self._ttl)
        if job.status == JobStatus.QUEUED:
            cancelled = job.model_copy(update={"status": JobStatus.CANCELLED})
        elif job.status == JobStatus.RUNNING:
            cancelled = job.model_copy(update={"status": JobStatus.CANCEL_REQUESTED})
        else:
            return job
        await self.update(cancelled)
        return cancelled

    async def cancellation_requested(self, job_id: str) -> bool:
        return bool(await self._redis.get(self._cancel_key(job_id)))

    async def close(self) -> None:
        await self._redis.aclose()

    @property
    def _queue_key(self) -> str:
        return f"{self._prefix}:expansion:queue"

    def _job_key(self, job_id: str) -> str:
        return f"{self._prefix}:expansion:job:{job_id}"

    def _payload_key(self, job_id: str) -> str:
        return f"{self._prefix}:expansion:payload:{job_id}"

    def _cancel_key(self, job_id: str) -> str:
        return f"{self._prefix}:expansion:cancel:{job_id}"

    @staticmethod
    def _text(value: Any) -> str:
        return value.decode() if isinstance(value, bytes) else str(value)


class RedisFixedWindowLimiter:
    """One fixed-window quota shared across replicas using an atomic Lua step."""

    _SCRIPT = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return {current, redis.call('TTL', KEYS[1])}
"""

    def __init__(self, redis_client: Any, limit_per_minute: int, *, prefix: str = "scholar"):
        self._redis = redis_client
        self.limit = limit_per_minute
        self._prefix = prefix.rstrip(":")

    @classmethod
    def from_url(
        cls,
        url: str,
        limit_per_minute: int,
        *,
        prefix: str = "scholar",
    ) -> RedisFixedWindowLimiter:
        try:
            from redis.asyncio import Redis
        except ImportError as exc:  # pragma: no cover - depends on optional install
            raise RuntimeError(
                "SCHOLAR_REDIS_URL requires installation with .[distributed]"
            ) from exc
        return cls(
            Redis.from_url(url, decode_responses=True),
            limit_per_minute,
            prefix=prefix,
        )

    async def admit(self, subject: str) -> tuple[bool, int]:
        if self.limit <= 0:
            return True, 0
        minute = int(time.time() // 60)
        # Do not place raw API keys into Redis key names.
        subject_hash = hashlib.sha256(subject.encode()).hexdigest()
        key = f"{self._prefix}:rate:{minute}:{subject_hash}"
        current, ttl = await self._redis.eval(self._SCRIPT, 1, key, 61)
        count = int(current)
        retry_after = max(1, int(ttl)) if count > self.limit else 0
        return count <= self.limit, retry_after

    async def close(self) -> None:
        await self._redis.aclose()


def encode_worker_error(exc: Exception) -> str:
    """Expose only a stable exception category through shared job state."""

    return type(exc).__name__
