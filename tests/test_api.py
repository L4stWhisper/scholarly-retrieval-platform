from __future__ import annotations

import asyncio

import httpx

from scholarly_retrieval.api import create_app
from scholarly_retrieval.models import (
    Paper,
    ProviderBatch,
    RelatedQuery,
    RetrievalMethod,
    SearchQuery,
    SourceRecord,
)
from scholarly_retrieval.providers.base import ProviderCapabilities, ScholarlyProvider
from scholarly_retrieval.service import ScholarService


class ApiFixtureProvider(ScholarlyProvider):
    name = "api_fixture"
    capabilities = ProviderCapabilities(
        keyword_search=True,
        resolve_id=True,
        references="list",
        citations="list",
        related=True,
    )

    async def search(self, query: SearchQuery) -> ProviderBatch:
        return ProviderBatch(papers=[Paper(record_id="api:1", title=query.text)])

    async def resolve(self, identifier: str) -> Paper | None:
        return Paper(record_id=f"api:{identifier}", title=identifier)

    async def references(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        return ProviderBatch(
            papers=[
                Paper(
                    record_id="api:ref",
                    title="Reference",
                    publication_year=2020,
                    work_type="article",
                )
            ]
        )

    async def citations(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        return ProviderBatch(papers=[Paper(record_id="api:cite", title="Citation")])

    async def related(self, query: RelatedQuery) -> ProviderBatch:
        return ProviderBatch(
            papers=[
                Paper(
                    record_id="api:related",
                    title="Related",
                    source_records=[
                        SourceRecord(
                            provider=self.name,
                            source_record_id="related",
                            provider_rank=1,
                            retrieval_method=RetrievalMethod.OPENALEX_SEMANTIC,
                        )
                    ],
                )
            ]
        )


def service_factory() -> ScholarService:
    return ScholarService([ApiFixtureProvider()])


def test_api_search_returns_the_library_result_contract() -> None:
    async def scenario() -> httpx.Response:
        app = create_app(service_factory)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                return await client.post(
                    "/v1/search",
                    json={
                        "text": "citation graph",
                        "limit": 1,
                        "sources": ["api_fixture"],
                    },
                )

    response = asyncio.run(scenario())

    assert response.status_code == 200
    payload = response.json()
    assert payload["papers"][0]["record_id"] == "api:1"
    assert payload["provider_reports"][0]["provider"] == "api_fixture"
    assert payload["fingerprint"].startswith("v1:")


def test_api_validation_and_service_errors_have_distinct_status_codes() -> None:
    async def scenario() -> tuple[httpx.Response, httpx.Response]:
        app = create_app(service_factory)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                invalid = await client.post("/v1/search", json={"text": " ", "limit": 1})
                unknown = await client.post(
                    "/v1/search",
                    json={"text": "query", "sources": ["unknown"]},
                )
                return invalid, unknown

    invalid, unknown = asyncio.run(scenario())

    assert invalid.status_code == 422
    assert unknown.status_code == 400
    assert unknown.json() == {"detail": "unknown providers: unknown"}


def test_api_openapi_exposes_all_implemented_operations() -> None:
    async def scenario() -> dict:
        app = create_app(service_factory)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                return (await client.get("/openapi.json")).json()["paths"]

    paths = asyncio.run(scenario())

    assert set(paths) >= {
        "/health",
        "/v1/providers",
        "/v1/search",
        "/v1/resolve",
        "/v1/related",
        "/v1/references/extract",
        "/v1/references/link",
        "/v1/references",
        "/v1/citations",
        "/v1/graph/expand",
        "/v1/jobs/expand",
        "/v1/jobs/{job_id}",
    }


def test_api_extracts_external_bibtex_without_promoting_verified_edges() -> None:
    async def scenario() -> httpx.Response:
        app = create_app(service_factory)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                return await client.post(
                    "/v1/references/extract",
                    json={
                        "source_format": "bibtex",
                        "content": "@article{x, title={Paper X}, year={2024}}",
                    },
                )

    response = asyncio.run(scenario())
    assert response.status_code == 200
    payload = response.json()
    assert payload["source_format"] == "bibtex"
    assert payload["references"][0]["evidence_level"] == "bibliography_only"


def test_api_reference_link_exposes_candidate_score_and_decision_reason() -> None:
    async def scenario() -> httpx.Response:
        app = create_app(service_factory)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                return await client.post(
                    "/v1/references/link",
                    json={
                        "seed_identifier": "seed",
                        "sources": ["api_fixture"],
                        "extraction": {
                            "source_format": "jats",
                            "bibliography_entry_count": 1,
                            "cited_entry_count": 1,
                            "references": [
                                {
                                    "reference_id": "R1",
                                    "ordinal": 1,
                                    "cited_in_text": True,
                                    "evidence_level": "verified_anchor",
                                    "raw_text": "Scored paper",
                                    "title": "Scored paper",
                                    "callout_count": 1,
                                }
                            ],
                        },
                    },
                )

    response = asyncio.run(scenario())
    assert response.status_code == 200
    link = response.json()["links"][0]
    assert link["status"] == "resolved"
    assert link["candidate_scores"][0]["total_score"] == 1.0
    assert link["decision_reason"] == "score_and_margin_satisfied"


def test_api_resolve_and_graph_routes_preserve_relation_direction() -> None:
    async def scenario() -> tuple[dict, dict, dict]:
        app = create_app(service_factory)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                request = {"identifier": "seed", "sources": ["api_fixture"]}
                resolved = (await client.post("/v1/resolve", json=request)).json()
                references = (await client.post("/v1/references", json=request)).json()
                citations = (await client.post("/v1/citations", json=request)).json()
                return resolved, references, citations

    resolved, references, citations = asyncio.run(scenario())
    assert resolved["papers"][0]["record_id"] == "api:seed"
    assert references["assertions"][0]["relation"] == "references"
    assert references["assertions"][0]["subject_record_id"] == "api:seed"
    assert citations["assertions"][0]["relation"] == "cites"
    assert citations["assertions"][0]["object_record_id"] == "api:seed"


def test_api_expand_returns_budget_and_discovery_contract() -> None:
    async def scenario() -> dict:
        app = create_app(service_factory)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/v1/graph/expand",
                    json={
                        "identifier": "seed",
                        "direction": "references",
                        "depth": 1,
                        "frontier_cap": 5,
                        "top_k": 5,
                        "per_node_limit": 5,
                        "sources": ["api_fixture"],
                    },
                )
                assert response.status_code == 200
                return response.json()

    payload = asyncio.run(scenario())
    assert payload["operation"] == "expand"
    assert payload["depth_reached"] == 1
    assert payload["discovery_paths"][1]["target_record_id"] == "api:ref"
    assert payload["edges"][0]["citing_record_id"] == "api:seed"


def test_api_expand_accepts_multiple_seeds_and_reports_shared_neighbor() -> None:
    async def scenario() -> dict:
        app = create_app(service_factory)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/v1/graph/expand",
                    json={
                        "identifiers": ["first", "second"],
                        "direction": "references",
                        "depth": 1,
                        "year_from": 2019,
                        "year_to": 2021,
                        "include_work_types": ["article"],
                        "sources": ["api_fixture"],
                    },
                )
                assert response.status_code == 200
                return response.json()

    payload = asyncio.run(scenario())
    assert {paper["record_id"] for paper in payload["seeds"]} == {
        "api:first",
        "api:second",
    }
    assert {
        path["seed_record_id"]
        for path in payload["discovery_paths"]
        if path["target_record_id"] == "api:ref"
    } == {"api:first", "api:second"}
    coupling = payload["bibliographic_couplings"][0]
    assert coupling["shared_record_ids"] == ["api:ref"]
    assert payload["filtered_candidate_count"] == 0


def test_api_related_returns_rrf_evidence() -> None:
    async def scenario() -> dict:
        app = create_app(service_factory)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/v1/related",
                    json={"text": "citation discovery", "sources": ["api_fixture"]},
                )
                assert response.status_code == 200
                return response.json()

    payload = asyncio.run(scenario())
    assert payload["papers"][0]["record_id"] == "api:related"
    assert payload["rankings"][0]["score_method"] == "rrf"


def test_api_key_and_per_subject_quota_guard_remote_routes() -> None:
    async def scenario() -> tuple[int, int, int, int]:
        app = create_app(
            service_factory,
            api_keys={"test-secret"},
            rate_limit_per_minute=1,
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                health = await client.get("/health")
                missing = await client.get("/v1/providers")
                allowed = await client.get(
                    "/v1/providers", headers={"Authorization": "Bearer test-secret"}
                )
                limited = await client.get("/v1/providers", headers={"x-api-key": "test-secret"})
                assert limited.headers["retry-after"]
                return (
                    health.status_code,
                    missing.status_code,
                    allowed.status_code,
                    limited.status_code,
                )

    assert asyncio.run(scenario()) == (200, 401, 200, 429)


def test_api_rejects_declared_oversized_request_before_parsing() -> None:
    async def scenario() -> httpx.Response:
        app = create_app(service_factory, rate_limit_per_minute=0)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                return await client.post(
                    "/v1/search",
                    content=b"{}",
                    headers={"content-length": str(5 * 1024 * 1024)},
                )

    response = asyncio.run(scenario())
    assert response.status_code == 413
    assert response.json() == {"detail": "request body too large"}


def test_api_long_expansion_job_can_be_polled_to_completion() -> None:
    async def scenario() -> dict:
        app = create_app(service_factory, rate_limit_per_minute=0)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                submitted = await client.post(
                    "/v1/jobs/expand",
                    json={
                        "identifier": "seed",
                        "direction": "references",
                        "depth": 1,
                        "sources": ["api_fixture"],
                    },
                )
                assert submitted.status_code == 202
                job_id = submitted.json()["job_id"]
                for _ in range(20):
                    payload = (await client.get(f"/v1/jobs/{job_id}")).json()
                    if payload["status"] not in {"queued", "running"}:
                        return payload
                    await asyncio.sleep(0)
                raise AssertionError("expansion job did not complete")

    payload = asyncio.run(scenario())
    assert payload["status"] == "complete"
    assert payload["result"]["papers"][1]["record_id"] == "api:ref"
