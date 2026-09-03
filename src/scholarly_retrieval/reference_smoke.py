"""Low-load real-data validation for JATS references and provider citation lists."""

from __future__ import annotations

import re

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .models import (
    IdentifierScheme,
    Paper,
    ProviderContribution,
    ProviderReport,
    ReferenceExtractionResult,
    ReferenceLinkStatus,
)
from .reference_extraction import extract_jats_references
from .reliability import ReliableHttpClient, RetryPolicy
from .service import ScholarService

EUROPE_PMC_FULLTEXT_URL = (
    "https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
)


class ReferenceSmokeItem(BaseModel):
    """Agreement for one body-cited DOI reference across two retrieval paths."""

    model_config = ConfigDict(extra="forbid")

    reference_id: str
    title: str | None = None
    doi: str
    provider_relation_present: bool
    deposited_metadata_present: bool
    link_status: ReferenceLinkStatus
    linked_target_record_id: str | None = None
    linked_target_dois: list[str] = Field(default_factory=list)
    linked_target_matches_doi: bool
    paths_agree: bool


class ReferenceSmokeResult(BaseModel):
    """Bounded functional evidence; this is deliberately not a scientific gold set."""

    model_config = ConfigDict(extra="forbid")

    seed_identifier: str
    pmcid: str
    bibliography_entry_count: int = Field(ge=0)
    cited_entry_count: int = Field(ge=0)
    bibliography_doi_count: int = Field(ge=0)
    cited_doi_count: int = Field(ge=0)
    cited_without_doi_count: int = Field(ge=0)
    cited_doi_fraction: float | None = Field(default=None, ge=0, le=1)
    provider_relation_record_count: int = Field(ge=0)
    provider_relation_doi_count: int = Field(ge=0)
    deposited_metadata_doi_count: int = Field(ge=0)
    provider_relation_doi_overlap: int = Field(ge=0)
    provider_relation_doi_recall: float | None = Field(default=None, ge=0, le=1)
    deposited_metadata_doi_overlap: int = Field(ge=0)
    deposited_metadata_doi_recall: float | None = Field(default=None, ge=0, le=1)
    combined_evidence_doi_overlap: int = Field(ge=0)
    combined_evidence_doi_recall: float | None = Field(default=None, ge=0, le=1)
    sample_size: int = Field(ge=0)
    link_success_count: int = Field(ge=0)
    path_agreement_count: int = Field(ge=0)
    graph_truncated: bool
    missing_provider_relation_dois: list[str] = Field(default_factory=list)
    missing_combined_evidence_dois: list[str] = Field(default_factory=list)
    items: list[ReferenceSmokeItem] = Field(default_factory=list)
    graph_provider_reports: list[ProviderReport] = Field(default_factory=list)
    graph_provider_contributions: list[ProviderContribution] = Field(default_factory=list)
    linking_provider_reports: list[ProviderReport] = Field(default_factory=list)
    interpretation: str = (
        "Functional smoke only: JATS DOI labels and bounded live provider responses are not "
        "an independently adjudicated completeness gold set."
    )


async def fetch_europe_pmc_jats(
    pmcid: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> str:
    """Fetch public Europe PMC JATS while rejecting HTML error pages."""

    normalized = pmcid.strip().upper()
    if not re.fullmatch(r"PMC\d+", normalized):
        raise ValueError("pmcid must look like PMC8371605")
    owns_client = client is None
    active_client = client or httpx.AsyncClient(timeout=httpx.Timeout(30.0))
    reliable = ReliableHttpClient(
        active_client,
        provider="europe_pmc_fulltext",
        policy=RetryPolicy(max_attempts=3, max_concurrency=1),
    )
    try:
        try:
            # Full-text XML is a read-only request, so retrying transient
            # transport failures, 429, and 5xx responses is safe. Validation
            # below still rejects a successful HTML/error body.
            response = await reliable.get(
                EUROPE_PMC_FULLTEXT_URL.format(pmcid=normalized),
                params={"format": "xml"},
                operation="fetch_jats",
                use_cache=False,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ValueError(f"Europe PMC full-text request failed: {exc}") from exc
        content_type = response.headers.get("content-type", "").casefold()
        text = response.text
        if "xml" not in content_type or "<article" not in text[:8192].casefold():
            raise ValueError("Europe PMC did not return JATS XML")
        return text
    finally:
        if owns_client:
            await active_client.aclose()


async def validate_reference_smoke(
    service: ScholarService,
    *,
    seed_identifier: str,
    pmcid: str,
    jats_xml: str,
    sources: list[str],
    sample_size: int = 5,
    relation_limit: int = 100,
) -> ReferenceSmokeResult:
    """Compare JATS DOI references with graph retrieval and direct entity linking."""

    if not 1 <= sample_size <= 20:
        raise ValueError("sample_size must be between 1 and 20")
    if not 1 <= relation_limit <= 1000:
        raise ValueError("relation_limit must be between 1 and 1000")
    extraction = extract_jats_references(jats_xml, include_uncited=True)
    cited_with_doi = [
        reference
        for reference in extraction.references
        if reference.cited_in_text and reference.doi is not None
    ]
    if not cited_with_doi:
        raise ValueError("JATS contains no body-cited DOI references")

    graph = await service.references(
        seed_identifier,
        limit=relation_limit,
        sources=sources,
    )
    graph_dois = {
        doi
        for paper in graph.papers
        for doi in _paper_dois(paper)
    }
    deposited_dois = {
        reference.doi
        for reference in graph.unresolved_references
        if reference.doi is not None
    }
    bibliography_dois = {
        reference.doi for reference in extraction.references if reference.doi
    }
    cited_dois = {reference.doi for reference in cited_with_doi if reference.doi}

    selected = cited_with_doi[:sample_size]
    selected_extraction = ReferenceExtractionResult(
        source_format=extraction.source_format,
        references=selected,
        bibliography_entry_count=len(selected),
        cited_entry_count=len(selected),
        warnings=extraction.warnings,
        processing_steps=extraction.processing_steps,
    )
    linking = await service.link_references(
        seed_identifier,
        selected_extraction,
        sources=sources,
    )
    links_by_id = {link.reference.reference_id: link for link in linking.links}
    items: list[ReferenceSmokeItem] = []
    for reference in selected:
        link = links_by_id[reference.reference_id]
        target = link.candidates[0] if link.status == ReferenceLinkStatus.RESOLVED else None
        target_dois = _paper_dois(target) if target is not None else []
        target_matches = reference.doi in target_dois
        relation_present = reference.doi in graph_dois
        metadata_present = reference.doi in deposited_dois
        items.append(
            ReferenceSmokeItem(
                reference_id=reference.reference_id,
                title=reference.title,
                doi=reference.doi or "",
                provider_relation_present=relation_present,
                deposited_metadata_present=metadata_present,
                link_status=link.status,
                linked_target_record_id=target.record_id if target is not None else None,
                linked_target_dois=target_dois,
                linked_target_matches_doi=target_matches,
                paths_agree=target_matches and (relation_present or metadata_present),
            )
        )

    graph_overlap = len(bibliography_dois & graph_dois)
    deposited_overlap = len(bibliography_dois & deposited_dois)
    combined_dois = graph_dois | deposited_dois
    combined_overlap = len(bibliography_dois & combined_dois)
    return ReferenceSmokeResult(
        seed_identifier=seed_identifier,
        pmcid=pmcid.strip().upper(),
        bibliography_entry_count=extraction.bibliography_entry_count,
        cited_entry_count=extraction.cited_entry_count,
        bibliography_doi_count=len(bibliography_dois),
        cited_doi_count=len(cited_dois),
        cited_without_doi_count=extraction.cited_entry_count - len(cited_with_doi),
        cited_doi_fraction=(
            len(cited_with_doi) / extraction.cited_entry_count
            if extraction.cited_entry_count
            else None
        ),
        provider_relation_record_count=len(graph.papers),
        provider_relation_doi_count=len(graph_dois),
        deposited_metadata_doi_count=len(deposited_dois),
        provider_relation_doi_overlap=graph_overlap,
        provider_relation_doi_recall=(
            graph_overlap / len(bibliography_dois) if bibliography_dois else None
        ),
        deposited_metadata_doi_overlap=deposited_overlap,
        deposited_metadata_doi_recall=(
            deposited_overlap / len(bibliography_dois) if bibliography_dois else None
        ),
        combined_evidence_doi_overlap=combined_overlap,
        combined_evidence_doi_recall=(
            combined_overlap / len(bibliography_dois) if bibliography_dois else None
        ),
        sample_size=len(items),
        link_success_count=sum(item.linked_target_matches_doi for item in items),
        path_agreement_count=sum(item.paths_agree for item in items),
        graph_truncated=graph.truncated,
        missing_provider_relation_dois=sorted(bibliography_dois - graph_dois),
        missing_combined_evidence_dois=sorted(bibliography_dois - combined_dois),
        items=items,
        graph_provider_reports=graph.provider_reports,
        graph_provider_contributions=graph.provider_contributions,
        linking_provider_reports=linking.provider_reports,
    )


def _paper_dois(paper: Paper | None) -> list[str]:
    if paper is None:
        return []
    return sorted(
        {
            claim.value
            for claim in paper.identifiers
            if claim.scheme == IdentifierScheme.DOI
        }
    )
