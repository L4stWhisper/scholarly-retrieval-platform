"""FastAPI adapter over the shared scholarly retrieval service."""

from __future__ import annotations

import asyncio
import hmac
import os
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from time import monotonic
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .config import load_environment
from .distributed import (
    ExpansionJob,
    ExpansionJobPayload,
    JobStatus,
    RedisExpansionJobStore,
    RedisFixedWindowLimiter,
)
from .models import (
    GraphExpansionQuery,
    GraphExpansionResult,
    GraphResult,
    ReferenceExtractionResult,
    ReferenceLinkingResult,
    RelatedQuery,
    RelatedResult,
    ResolveResult,
    SearchQuery,
    SearchResult,
)
from .reference_extraction import extract_references_from_text
from .service import ScholarService

ServiceFactory = Callable[[], ScholarService]


class SearchRequest(SearchQuery):
    sources: list[str] | None = None


class ResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identifier: str = Field(min_length=1)
    sources: list[str] | None = None

    @field_validator("identifier")
    @classmethod
    def identifier_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("identifier must not be blank")
        return normalized


class RelationRequest(ResolveRequest):
    limit: int = Field(default=100, ge=1, le=1000)


class ExpansionRequest(GraphExpansionQuery):
    sources: list[str] | None = None


class RelatedRequest(RelatedQuery):
    sources: list[str] | None = None


class ReferenceLinkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seed_identifier: str = Field(min_length=1)
    extraction: ReferenceExtractionResult
    sources: list[str] | None = None


class ReferenceExtractRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1)
    source_format: str = Field(pattern="^(?i:jats|tei|latex|bibtex)$")
    include_uncited: bool = False


@dataclass
class _RateWindow:
    started_at: float
    count: int


class _FixedWindowLimiter:
    """Small single-process guard; production replicas need a shared gateway."""

    def __init__(self, limit_per_minute: int) -> None:
        self.limit = limit_per_minute
        self._windows: dict[str, _RateWindow] = {}

    async def admit(self, subject: str) -> tuple[bool, int]:
        if self.limit <= 0:
            return True, 0
        now = monotonic()
        window = self._windows.get(subject)
        if window is None or now - window.started_at >= 60:
            self._windows[subject] = _RateWindow(now, 1)
            return True, 0
        if window.count >= self.limit:
            return False, max(1, int(60 - (now - window.started_at)))
        window.count += 1
        return True, 0


def create_app(
    service_factory: ServiceFactory = ScholarService,
    *,
    api_keys: set[str] | None = None,
    rate_limit_per_minute: int | None = None,
) -> FastAPI:
    """Create an app whose lifespan owns exactly one application service."""

    load_environment()
    configured_keys = api_keys
    if configured_keys is None:
        configured_keys = {
            value.strip() for value in os.getenv("SCHOLAR_API_KEYS", "").split(",") if value.strip()
        }
    configured_limit = (
        rate_limit_per_minute
        if rate_limit_per_minute is not None
        else int(os.getenv("SCHOLAR_RATE_LIMIT_PER_MINUTE", "60"))
    )
    max_request_body_bytes = int(
        os.getenv("SCHOLAR_API_MAX_REQUEST_BODY_BYTES", str(4 * 1024 * 1024))
    )
    if max_request_body_bytes < 1:
        raise ValueError("SCHOLAR_API_MAX_REQUEST_BODY_BYTES must be positive")
    redis_url = os.getenv("SCHOLAR_REDIS_URL", "").strip()
    redis_prefix = os.getenv("SCHOLAR_REDIS_PREFIX", "scholar")
    distributed_jobs = bool(redis_url)
    if redis_url:
        limiter = RedisFixedWindowLimiter.from_url(
            redis_url,
            configured_limit,
            prefix=redis_prefix,
        )
        shared_job_store = RedisExpansionJobStore.from_url(
            redis_url,
            prefix=redis_prefix,
            ttl_seconds=int(os.getenv("SCHOLAR_JOB_TTL_SECONDS", "86400")),
        )
    else:
        limiter = _FixedWindowLimiter(configured_limit)
        shared_job_store = None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.scholar_service = service_factory()
        app.state.expansion_jobs: dict[str, ExpansionJob] = {}
        app.state.expansion_tasks: dict[str, asyncio.Task[None]] = {}
        app.state.shared_job_store = shared_job_store
        try:
            yield
        finally:
            for task in app.state.expansion_tasks.values():
                if not task.done():
                    task.cancel()
            if app.state.expansion_tasks:
                await asyncio.gather(*app.state.expansion_tasks.values(), return_exceptions=True)
            await app.state.scholar_service.close()
            if shared_job_store is not None:
                await shared_job_store.close()
                await limiter.close()

    app = FastAPI(
        title="Scholarly Retrieval Platform",
        version="0.1.0",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def security_boundary(request: Request, call_next):
        if request.url.path == "/health":
            return await call_next(request)
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError:
                return JSONResponse(status_code=400, content={"detail": "invalid Content-Length"})
            if declared_length > max_request_body_bytes:
                return JSONResponse(status_code=413, content={"detail": "request body too large"})
        supplied = request.headers.get("x-api-key")
        authorization = request.headers.get("authorization", "")
        if supplied is None and authorization.casefold().startswith("bearer "):
            supplied = authorization[7:].strip()
        if configured_keys and not any(
            supplied is not None and hmac.compare_digest(supplied, expected)
            for expected in configured_keys
        ):
            return JSONResponse(status_code=401, content={"detail": "invalid API key"})
        subject = supplied or (request.client.host if request.client is not None else "anonymous")
        admitted, retry_after = await limiter.admit(subject)
        if not admitted:
            return JSONResponse(
                status_code=429,
                content={"detail": "request quota exceeded"},
                headers={"Retry-After": str(retry_after)},
            )
        return await call_next(request)

    @app.exception_handler(ValueError)
    async def value_error_handler(_request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(LookupError)
    async def lookup_error_handler(_request: Request, exc: LookupError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.get("/health")
    async def health(http_request: Request) -> dict:
        service: ScholarService = http_request.app.state.scholar_service
        return {"status": "ok", "providers": sorted(service.providers)}

    @app.get("/v1/providers")
    async def providers(http_request: Request) -> dict[str, dict]:
        service: ScholarService = http_request.app.state.scholar_service
        return service.provider_capabilities()

    @app.post("/v1/search", response_model=SearchResult)
    async def search(request: SearchRequest, http_request: Request) -> SearchResult:
        service: ScholarService = http_request.app.state.scholar_service
        query = SearchQuery.model_validate(request.model_dump(exclude={"sources"}))
        return await service.search(query, sources=request.sources)

    @app.post("/v1/resolve", response_model=ResolveResult)
    async def resolve(request: ResolveRequest, http_request: Request) -> ResolveResult:
        service: ScholarService = http_request.app.state.scholar_service
        return await service.resolve(request.identifier, sources=request.sources)

    @app.post("/v1/related", response_model=RelatedResult)
    async def related(request: RelatedRequest, http_request: Request) -> RelatedResult:
        service: ScholarService = http_request.app.state.scholar_service
        query = RelatedQuery.model_validate(request.model_dump(exclude={"sources"}))
        return await service.related(query, sources=request.sources)

    @app.post("/v1/references/link", response_model=ReferenceLinkingResult)
    async def link_references(
        request: ReferenceLinkRequest, http_request: Request
    ) -> ReferenceLinkingResult:
        service: ScholarService = http_request.app.state.scholar_service
        return await service.link_references(
            request.seed_identifier,
            request.extraction,
            sources=request.sources,
        )

    @app.post("/v1/references/extract", response_model=ReferenceExtractionResult)
    async def extract_references(request: ReferenceExtractRequest) -> ReferenceExtractionResult:
        """Extract references from authorized text content without filesystem access."""

        return extract_references_from_text(
            request.content,
            source_format=request.source_format,
            include_uncited=request.include_uncited,
        )

    @app.post("/v1/references", response_model=GraphResult)
    async def references(request: RelationRequest, http_request: Request) -> GraphResult:
        service: ScholarService = http_request.app.state.scholar_service
        return await service.references(
            request.identifier,
            limit=request.limit,
            sources=request.sources,
        )

    @app.post("/v1/citations", response_model=GraphResult)
    async def citations(request: RelationRequest, http_request: Request) -> GraphResult:
        service: ScholarService = http_request.app.state.scholar_service
        return await service.citations(
            request.identifier,
            limit=request.limit,
            sources=request.sources,
        )

    @app.post("/v1/graph/expand", response_model=GraphExpansionResult)
    async def expand(request: ExpansionRequest, http_request: Request) -> GraphExpansionResult:
        service: ScholarService = http_request.app.state.scholar_service
        query = GraphExpansionQuery.model_validate(request.model_dump(exclude={"sources"}))
        return await service.expand(query, sources=request.sources)

    async def run_expansion_job(
        job_id: str, request: ExpansionRequest, service: ScholarService
    ) -> None:
        jobs: dict[str, ExpansionJob] = app.state.expansion_jobs
        jobs[job_id] = jobs[job_id].model_copy(update={"status": JobStatus.RUNNING})
        try:
            query = GraphExpansionQuery.model_validate(request.model_dump(exclude={"sources"}))
            result = await service.expand(query, sources=request.sources)
        except asyncio.CancelledError:
            jobs[job_id] = jobs[job_id].model_copy(update={"status": JobStatus.CANCELLED})
            raise
        except Exception as exc:
            # Long-task status exposes a stable category, never provider URLs,
            # credentials, or arbitrary exception messages.
            jobs[job_id] = jobs[job_id].model_copy(
                update={"status": JobStatus.FAILED, "error_code": type(exc).__name__}
            )
        else:
            jobs[job_id] = jobs[job_id].model_copy(
                update={"status": JobStatus.COMPLETE, "result": result}
            )

    @app.post("/v1/jobs/expand", response_model=ExpansionJob, status_code=202)
    async def submit_expansion_job(
        request: ExpansionRequest, http_request: Request
    ) -> ExpansionJob:
        job_id = str(uuid.uuid4())
        job = ExpansionJob(job_id=job_id, status=JobStatus.QUEUED)
        if distributed_jobs:
            query_data = request.model_dump(exclude={"sources"}, mode="json")
            await http_request.app.state.shared_job_store.enqueue(
                job,
                ExpansionJobPayload(request=query_data, sources=request.sources),
            )
            return job
        http_request.app.state.expansion_jobs[job_id] = job
        service: ScholarService = http_request.app.state.scholar_service
        task = asyncio.create_task(run_expansion_job(job_id, request, service))
        http_request.app.state.expansion_tasks[job_id] = task
        return job

    @app.get("/v1/jobs/{job_id}", response_model=ExpansionJob)
    async def get_expansion_job(job_id: str, http_request: Request) -> ExpansionJob:
        if distributed_jobs:
            job = await http_request.app.state.shared_job_store.get(job_id)
        else:
            job = http_request.app.state.expansion_jobs.get(job_id)
        if job is None:
            raise LookupError(f"job not found: {job_id}")
        return job

    @app.delete("/v1/jobs/{job_id}", response_model=ExpansionJob)
    async def cancel_expansion_job(job_id: str, http_request: Request) -> ExpansionJob:
        if distributed_jobs:
            job = await http_request.app.state.shared_job_store.request_cancel(job_id)
            if job is None:
                raise LookupError(f"job not found: {job_id}")
            return job
        job = http_request.app.state.expansion_jobs.get(job_id)
        if job is None:
            raise LookupError(f"job not found: {job_id}")
        task: asyncio.Task[Any] | None = http_request.app.state.expansion_tasks.get(job_id)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return http_request.app.state.expansion_jobs[job_id]

    return app


app = create_app()


def main() -> None:
    """Run the HTTP adapter; loopback is the safe default without authentication."""

    host = os.getenv("SCHOLAR_API_HOST", "127.0.0.1")
    port = int(os.getenv("SCHOLAR_API_PORT", "8000"))
    uvicorn.run("scholarly_retrieval.api:app", host=host, port=port)
