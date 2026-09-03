"""Redis-backed graph-expansion worker for multi-replica deployments."""

from __future__ import annotations

import asyncio
import os
from typing import Any

from .config import load_environment
from .distributed import (
    ExpansionJob,
    JobStatus,
    RedisExpansionJobStore,
    encode_worker_error,
)
from .models import GraphExpansionQuery
from .service import ScholarService


async def execute_expansion_job(
    job_id: str,
    *,
    store: Any,
    service: ScholarService,
    cancel_poll_seconds: float = 0.5,
) -> None:
    """Claim and execute one job while honoring cancellation from any API replica."""

    claimed = await store.mark_running(job_id)
    if claimed is None:
        return
    payload = await store.payload(job_id)
    if payload is None:
        await store.update(
            ExpansionJob(
                job_id=job_id,
                status=JobStatus.FAILED,
                error_code="MissingJobPayload",
            )
        )
        return

    try:
        query = GraphExpansionQuery.model_validate(payload.request)
    except Exception as exc:
        await store.update(
            claimed.model_copy(
                update={"status": JobStatus.FAILED, "error_code": encode_worker_error(exc)}
            )
        )
        return

    task = asyncio.create_task(service.expand(query, sources=payload.sources))
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=cancel_poll_seconds)
            current = await store.get(job_id)
            if await store.cancellation_requested(job_id):
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                current = current or claimed
                await store.update(
                    current.model_copy(update={"status": JobStatus.CANCELLED})
                )
                return
        result = await task
    except asyncio.CancelledError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise
    except Exception as exc:
        await store.update(
            claimed.model_copy(
                update={"status": JobStatus.FAILED, "error_code": encode_worker_error(exc)}
            )
        )
    else:
        if await store.cancellation_requested(job_id):
            current = await store.get(job_id) or claimed
            await store.update(current.model_copy(update={"status": JobStatus.CANCELLED}))
        else:
            await store.update(
                claimed.model_copy(update={"status": JobStatus.COMPLETE, "result": result})
            )


async def run_worker(*, once: bool = False) -> None:
    load_environment()
    redis_url = os.getenv("SCHOLAR_REDIS_URL", "").strip()
    if not redis_url:
        raise ValueError("SCHOLAR_REDIS_URL is required for scholar-worker")
    prefix = os.getenv("SCHOLAR_REDIS_PREFIX", "scholar")
    ttl = int(os.getenv("SCHOLAR_JOB_TTL_SECONDS", "86400"))
    store = RedisExpansionJobStore.from_url(redis_url, prefix=prefix, ttl_seconds=ttl)
    service = ScholarService()
    try:
        while True:
            job_id = await store.dequeue(timeout_seconds=5)
            if job_id is not None:
                await execute_expansion_job(job_id, store=store, service=service)
                if once:
                    return
            elif once:
                return
    finally:
        await service.close()
        await store.close()


def main() -> None:
    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        pass
