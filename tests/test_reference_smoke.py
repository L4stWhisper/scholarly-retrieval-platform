from __future__ import annotations

import asyncio
import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
from typer.testing import CliRunner

import scholarly_retrieval.cli as cli_module
from scholarly_retrieval.cli import app
from scholarly_retrieval.models import (
    IdentifierClaim,
    IdentifierScheme,
    Paper,
    Provenance,
    ProviderBatch,
    SearchQuery,
)
from scholarly_retrieval.providers.base import ProviderCapabilities, ScholarlyProvider
from scholarly_retrieval.reference_smoke import (
    fetch_europe_pmc_jats,
    validate_reference_smoke,
)
from scholarly_retrieval.service import ScholarService

runner = CliRunner()

JATS = """<!DOCTYPE article PUBLIC "-//NLM//DTD JATS 1.2//EN" "JATS.dtd">
<article><body><p>Prior <xref ref-type="bibr" rid="R1">1</xref> and
<xref ref-type="bibr" rid="R2">2</xref>.</p></body><back><ref-list>
<ref id="R1"><element-citation><article-title>First Work</article-title><year>2020</year>
<pub-id pub-id-type="doi">10.1234/first</pub-id></element-citation></ref>
<ref id="R2"><element-citation><article-title>Second Work</article-title><year>2021</year>
<pub-id pub-id-type="doi">10.1234/second</pub-id></element-citation></ref>
<ref id="R3"><element-citation><article-title>Uncited Work</article-title><year>2019</year>
<pub-id pub-id-type="doi">10.1234/uncited</pub-id></element-citation></ref>
</ref-list></back></article>"""


def _paper(record_id: str, title: str, doi: str) -> Paper:
    provenance = Provenance(provider="smoke", source_record_id=record_id)
    return Paper(
        record_id=record_id,
        title=title,
        identifiers=[
            IdentifierClaim(
                scheme=IdentifierScheme.DOI,
                value=doi,
                provenance=provenance,
            )
        ],
    )


class SmokeProvider(ScholarlyProvider):
    name = "smoke"
    capabilities = ProviderCapabilities(
        resolve_id=True,
        references="list",
        citations="none",
    )

    async def search(self, query: SearchQuery) -> ProviderBatch:
        return ProviderBatch()

    async def resolve(self, identifier: str) -> Paper | None:
        normalized = identifier.casefold()
        if "seed" in normalized:
            return _paper("smoke:seed", "Seed", "10.1234/seed")
        if "first" in normalized:
            return _paper("smoke:first", "First Work", "10.1234/first")
        if "second" in normalized:
            return _paper("smoke:second", "Second Work", "10.1234/second")
        return None

    async def references(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        return ProviderBatch(
            papers=[_paper("smoke:first", "First Work", "10.1234/first")]
        )

    async def citations(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        return ProviderBatch()


def test_reference_smoke_separates_graph_coverage_from_direct_link_success() -> None:
    async def scenario() -> None:
        service = ScholarService([SmokeProvider()])
        try:
            result = await validate_reference_smoke(
                service,
                seed_identifier="10.1234/seed",
                pmcid="PMC1",
                jats_xml=JATS,
                sources=["smoke"],
                sample_size=2,
                relation_limit=10,
            )
        finally:
            await service.close()

        assert result.bibliography_entry_count == 3
        assert result.cited_entry_count == 2
        assert result.bibliography_doi_count == 3
        assert result.cited_doi_count == 2
        assert result.cited_without_doi_count == 0
        assert result.provider_relation_doi_overlap == 1
        assert result.provider_relation_doi_recall == 1 / 3
        assert result.link_success_count == 2
        assert result.path_agreement_count == 1
        assert result.missing_provider_relation_dois == [
            "10.1234/second",
            "10.1234/uncited",
        ]
        assert result.items[1].linked_target_matches_doi is True
        assert result.items[1].provider_relation_present is False

    asyncio.run(scenario())


def test_fetch_europe_pmc_jats_checks_query_and_content_type() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/PMC8371605/fullTextXML")
            assert parse_qs(request.url.query.decode())["format"] == ["xml"]
            return httpx.Response(
                200,
                headers={"Content-Type": "application/xml"},
                text=JATS,
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        content = await fetch_europe_pmc_jats("pmc8371605", client=client)
        await client.aclose()
        assert "<article>" in content

    asyncio.run(scenario())


def test_fetch_europe_pmc_jats_retries_transient_gateway_failure() -> None:
    async def scenario() -> None:
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx.Response(504, request=request)
            return httpx.Response(
                200,
                headers={"Content-Type": "application/xml"},
                text=JATS,
                request=request,
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        content = await fetch_europe_pmc_jats("PMC8371605", client=client)
        await client.aclose()

        assert attempts == 2
        assert "<article>" in content

    asyncio.run(scenario())


def test_validate_reference_smoke_cli_uses_public_jats_and_selected_sources(
    monkeypatch,
) -> None:
    async def fake_fetch(pmcid: str) -> str:
        assert pmcid == "PMC1"
        return JATS

    monkeypatch.setattr(cli_module, "fetch_europe_pmc_jats", fake_fetch)
    monkeypatch.setattr(
        cli_module,
        "ScholarService",
        lambda: ScholarService([SmokeProvider()]),
    )

    result = runner.invoke(
        app,
        [
            "validate-reference-smoke",
            "10.1234/seed",
            "PMC1",
            "--source",
            "smoke",
            "--sample-size",
            "2",
            "--relation-limit",
            "10",
        ],
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["bibliography_doi_count"] == 3
    assert payload["cited_doi_count"] == 2
    assert payload["provider_relation_doi_recall"] == 1 / 3
    assert payload["link_success_count"] == 2


def test_committed_reference_smoke_case_is_auditable_and_not_called_gold() -> None:
    path = Path(__file__).parents[1] / "benchmarks" / "reference-smoke-cases.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    case = manifest["cases"][0]

    assert "not an adjudicated scientific gold set" in manifest["purpose"]
    assert case["seed_identifier"] == "10.1038/s41586-021-03819-2"
    assert case["pmcid"] == "PMC8371605"
    assert len(case["sample"]) == 5
    assert len({item["doi"] for item in case["sample"]}) == 5
