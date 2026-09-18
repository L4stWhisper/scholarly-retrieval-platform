"""Application service shared by CLI, HTTP API, and MCP adapters."""

from __future__ import annotations

import asyncio
import math
import os
import re
from collections.abc import Awaitable, Callable
from itertools import combinations
from pathlib import Path
from time import monotonic
from typing import TypeVar

import httpx

from .config import credential_status, load_environment
from .entities import build_entity_view
from .fingerprint import stable_fingerprint
from .identity import resolve_identities
from .models import (
    CitationAssertion,
    CitationEvidenceType,
    CitationVerificationStatus,
    DiscoveryPath,
    EntityRelationKind,
    ExpansionDirection,
    ExtractedReference,
    GraphExpansionQuery,
    GraphExpansionResult,
    GraphFilterExclusion,
    GraphFilterReason,
    GraphNodeRanking,
    GraphResult,
    GraphSimilarity,
    GraphSimilarityKind,
    IdentifierScheme,
    IdentityDecision,
    IdentityEvent,
    IdentityEventAction,
    Paper,
    Provenance,
    ProviderBatch,
    ProviderContribution,
    ProviderOverlap,
    ProviderReport,
    ReferenceEvidenceLevel,
    ReferenceExtractionResult,
    ReferenceLink,
    ReferenceLinkingResult,
    ReferenceLinkStatus,
    RelatedQuery,
    RelatedRanking,
    RelatedResult,
    RelationKind,
    ResolveResult,
    RetrievalEvidence,
    RunStatus,
    SearchQuery,
    SearchResult,
    SearchSort,
    UnresolvedReference,
    VisibleCitationEdge,
)
from .providers import (
    ProviderOperationError,
    ProviderRegistry,
    ScholarlyProvider,
    default_provider_registry,
)
from .query_language import matches_expression
from .reference_matching import (
    ReferenceMatchingPolicy,
    decide_reference_match,
    rank_reference_candidates,
)
from .reliability import current_run_id
from .storage import SQLiteStore, make_checkpoint_key

ResultT = TypeVar(
    "ResultT",
    SearchResult,
    RelatedResult,
    ResolveResult,
    GraphResult,
    GraphExpansionResult,
    ReferenceLinkingResult,
)
REFERENCE_LINK_CONCURRENCY = 4
SEARCH_RRF_K = 60
SEARCH_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "by",
        "for",
        "from",
        "in",
        "of",
        "on",
        "the",
        "to",
        "using",
        "via",
        "with",
    }
)


class ScholarService:
    def __init__(
        self,
        providers: list[ScholarlyProvider] | None = None,
        *,
        registry: ProviderRegistry | None = None,
        database_path: str | Path | None = None,
        clock: Callable[[], float] = monotonic,
        reference_matching_policy: ReferenceMatchingPolicy | None = None,
    ) -> None:
        self.environment_file = load_environment()
        configured_path = database_path or os.getenv("SCHOLAR_DB_PATH")
        self.store = SQLiteStore(configured_path) if configured_path else None
        if providers is not None and registry is not None:
            raise ValueError("pass providers or registry, not both")
        if providers is None:
            providers = (registry or default_provider_registry()).create_all(self.store)
        names = [provider.name for provider in providers]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"duplicate providers: {', '.join(duplicates)}")
        self.providers = {provider.name: provider for provider in providers}
        self._clock = clock
        self.reference_matching_policy = reference_matching_policy or ReferenceMatchingPolicy(
            auto_match_threshold=float(os.getenv("SCHOLAR_REFERENCE_AUTO_MATCH_THRESHOLD", "0.92")),
            minimum_margin=float(os.getenv("SCHOLAR_REFERENCE_MINIMUM_MARGIN", "0.08")),
        )

    def provider_capabilities(self) -> dict[str, dict]:
        return {
            name: provider.capabilities.model_dump()
            for name, provider in sorted(self.providers.items())
        }

    def configuration_status(self) -> dict:
        """Expose configuration presence without returning credential values."""

        if self.store is None:
            storage = {"enabled": False, "mode": None, "database_path": None}
        elif self.store.path == ":memory:":
            storage = {"enabled": True, "mode": "memory", "database_path": ":memory:"}
        else:
            # Resolve the configured path so CLI/API/MCP users can locate the
            # evidence database even when SCHOLAR_DB_PATH was relative.
            storage = {
                "enabled": True,
                "mode": "sqlite",
                "database_path": str(Path(self.store.path).resolve()),
            }
        return {
            "environment_file": (
                str(self.environment_file) if self.environment_file is not None else None
            ),
            "storage": storage,
            "credentials": credential_status(),
            "reference_matching_policy": {
                "auto_match_threshold": self.reference_matching_policy.auto_match_threshold,
                "minimum_margin": self.reference_matching_policy.minimum_margin,
            },
        }

    def plan_search(self, query: SearchQuery, *, sources: list[str] | None = None) -> dict:
        """Describe provider/local filter execution without making HTTP calls."""

        selected = self._select(sources)
        requested = {
            "text": True,
            "year_from": query.year_from is not None,
            "year_to": query.year_to is not None,
            "author": query.author is not None,
            "open_access": query.open_access is not None,
            "title": query.title is not None,
            "abstract": query.abstract is not None,
            "venue": query.venue is not None,
            "field": query.field is not None,
            "work_types": bool(query.work_types),
            "min_citations": query.min_citations is not None,
            "sort": query.sort != SearchSort.RELEVANCE,
            "expression": query.expression is not None,
        }
        local_defaults = {
            "year_from",
            "year_to",
            "author",
            "open_access",
            "title",
            "abstract",
            "venue",
            "field",
            "work_types",
            "min_citations",
            "sort",
            "expression",
        }
        # Fetch a larger candidate window from every source before canonical
        # fusion. Returning only query.limit from each source makes a source's
        # top-N omissions unrecoverable and materially lowers recall.
        provider_limit = min(100, query.limit * 3)
        plans = []
        for provider in selected:
            # Some provider filters are conditional on the requested value.
            # Ask the adapter for the executable plan instead of assuming that
            # every field has an unconditional, static capability.
            declared = provider.plan_search_filter_execution(query)
            execution = {
                field: declared.get(field, "local" if field in local_defaults else "unsupported")
                for field, enabled in requested.items()
                if enabled
            }
            plans.append(
                {
                    "provider": provider.name,
                    "execution": execution,
                    "provider_limit": provider_limit,
                    "pagination": provider.capabilities.pagination.get("search"),
                    "access_tier": provider.capabilities.access_tier,
                    "credential_variables": provider.capabilities.credential_variables,
                }
            )
        return {
            "query": query.model_dump(mode="json"),
            "fanout": len(plans),
            "plans": plans,
            "notes": [
                "providers overfetch up to 3x before deduplication and relevance fusion",
                "local filters run after bounded provider recall",
                "unsupported filters are disclosed and never treated as provider-executed",
            ],
        }

    def storage_stats(self) -> dict[str, int] | None:
        return self.store.stats() if self.store else None

    def storage_maintenance(
        self,
        *,
        retention_days: int = 30,
        max_raw_responses: int | None = None,
        apply: bool = False,
    ) -> dict:
        if self.store is None:
            raise ValueError("SCHOLAR_DB_PATH is required for storage maintenance")
        return self.store.prune(
            retention_days=retention_days,
            max_raw_responses=max_raw_responses,
            apply=apply,
        )

    def citation_evidence(self, edge_id: str) -> dict:
        """Return one persisted visible edge and its verification history."""

        if self.store is None:
            raise ValueError("SCHOLAR_DB_PATH is required for citation evidence replay")
        edge = self.store.get_citation_edge(edge_id.strip())
        if edge is None:
            raise LookupError(f"citation edge not found: {edge_id}")
        return {
            "edge": edge.model_dump(mode="json"),
            "status_events": [
                event.model_dump(mode="json")
                for event in self.store.list_citation_edge_events(edge_id=edge.edge_id)
            ],
        }

    def record_identity_event(
        self,
        *,
        action: IdentityEventAction,
        relation: EntityRelationKind,
        left_record_id: str,
        right_record_id: str,
        reasons: list[str],
    ) -> IdentityEvent:
        if self.store is None:
            raise ValueError("SCHOLAR_DB_PATH is required for identity review")
        return self.store.record_identity_event(
            action=action,
            relation=relation,
            left_record_id=left_record_id,
            right_record_id=right_record_id,
            reasons=reasons,
        )

    def identity_events(self, *, active_only: bool = False) -> list[IdentityEvent]:
        if self.store is None:
            raise ValueError("SCHOLAR_DB_PATH is required for identity review")
        return self.store.list_identity_events(active_only=active_only)

    def revert_identity_event(self, event_id: str, *, reason: str) -> IdentityEvent:
        if self.store is None:
            raise ValueError("SCHOLAR_DB_PATH is required for identity review")
        return self.store.revert_identity_event(event_id, reason=reason)

    async def search(self, query: SearchQuery, *, sources: list[str] | None = None) -> SearchResult:
        return await self._audited(
            "search",
            {"query": query.model_dump(mode="json"), "sources": sources},
            lambda: self._search(query, sources=sources),
        )

    async def _search(
        self, query: SearchQuery, *, sources: list[str] | None = None
    ) -> SearchResult:
        selected = self._select(sources)
        # Some graph/metadata providers deliberately expose no keyword index.
        # Capability routing prevents an unsupported call from being mistaken
        # for a successful empty search.
        active = [provider for provider in selected if provider.capabilities.keyword_search]
        reports = [
            ProviderReport(
                provider=provider.name,
                operation="search",
                status=RunStatus.SKIPPED,
                error_code="capability_none",
                error_message="provider does not expose keyword search",
            )
            for provider in selected
            if not provider.capabilities.keyword_search
        ]
        # Candidate overfetch serves recall for plain keyword search as well as
        # for locally filtered advanced search. The final limit is applied only
        # after cross-source identity resolution and relevance fusion.
        provider_query = query.model_copy(update={"limit": min(100, query.limit * 3)})
        outputs = await asyncio.gather(
            *(self._call(provider, "search", provider_query) for provider in active),
        )
        papers: list[Paper] = []
        truncated = False
        for batch, report in outputs:
            papers.extend(batch.papers)
            execution = dict(report.filter_execution)
            for field, enabled in {
                "year_from": query.year_from is not None,
                "year_to": query.year_to is not None,
                "author": query.author is not None,
                "open_access": query.open_access is not None,
                "title": query.title is not None,
                "abstract": query.abstract is not None,
                "venue": query.venue is not None,
                "field": query.field is not None,
                "work_types": bool(query.work_types),
                "min_citations": query.min_citations is not None,
                "sort": query.sort != SearchSort.RELEVANCE,
                "expression": query.expression is not None,
            }.items():
                if enabled and field not in execution:
                    execution[field] = "local"
            reports.append(report.model_copy(update={"filter_execution": execution}))
            truncated |= batch.truncated
        resolution = self._resolve_identities(papers)
        canonical = [
            paper for paper in resolution.papers if self._matches_search_filters(paper, query)
        ]
        canonical = self._sort_search_results(canonical, query.sort, query=query)
        truncated |= len(canonical) > query.limit
        canonical = canonical[: query.limit]
        return SearchResult(
            query=query,
            status=self._overall_status(reports, bool(canonical)),
            papers=canonical,
            provider_reports=reports,
            identity_decisions=resolution.decisions,
            raw_record_count=len(papers),
            canonical_record_count=len(canonical),
            entity_view=build_entity_view(canonical),
            truncated=truncated,
        )

    async def resolve(self, identifier: str, *, sources: list[str] | None = None) -> ResolveResult:
        identifier = self._validated_identifier(identifier)
        return await self._audited(
            "resolve",
            {"identifier": identifier, "sources": sources},
            lambda: self._resolve(identifier, sources=sources),
        )

    async def _resolve(self, identifier: str, *, sources: list[str] | None = None) -> ResolveResult:
        # Resolve is identity lookup, not keyword search: every selected provider
        # gets the same external ID and the shared identity layer reconciles hits.
        selected = self._select(sources)
        outputs = await asyncio.gather(
            *(self._call(provider, "resolve", identifier) for provider in selected)
        )
        papers = [paper for batch, _ in outputs for paper in batch.papers]
        reports = [report for _, report in outputs]
        resolution = self._resolve_identities(papers)
        canonical = resolution.papers
        return ResolveResult(
            identifier=identifier,
            status=self._overall_status(reports, bool(canonical)),
            papers=canonical,
            provider_reports=reports,
            identity_decisions=resolution.decisions,
            entity_view=build_entity_view(canonical),
        )

    async def related(
        self,
        query: RelatedQuery,
        *,
        sources: list[str] | None = None,
    ) -> RelatedResult:
        return await self._audited(
            "related",
            {"query": query.model_dump(mode="json"), "sources": sources},
            lambda: self._related(query, sources=sources),
        )

    async def link_references(
        self,
        seed_identifier: str,
        extraction: ReferenceExtractionResult,
        *,
        sources: list[str] | None = None,
    ) -> ReferenceLinkingResult:
        """Resolve extracted bibliography items and emit only verified visible edges."""

        seed_identifier = self._validated_identifier(seed_identifier)
        return await self._audited(
            "link_references",
            {
                "seed_identifier": seed_identifier,
                "extraction": extraction.model_dump(mode="json"),
                "sources": sources,
            },
            lambda: self._link_references(seed_identifier, extraction, sources=sources),
        )

    async def _link_references(
        self,
        seed_identifier: str,
        extraction: ReferenceExtractionResult,
        *,
        sources: list[str] | None,
    ) -> ReferenceLinkingResult:
        seed_result = await self._resolve(seed_identifier, sources=sources)
        if not seed_result.papers:
            raise LookupError(f"paper not found: {seed_identifier}")
        seed = seed_result.papers[0]
        links: list[ReferenceLink] = []
        reports = list(seed_result.provider_reports)
        assertions: list[CitationAssertion] = []

        # A large bibliography must not serialize every provider fan-out, but
        # unbounded concurrency would defeat provider pacing and rate limits.
        semaphore = asyncio.Semaphore(REFERENCE_LINK_CONCURRENCY)

        async def bounded_link(reference: ExtractedReference) -> ReferenceLink:
            async with semaphore:
                return await self._link_reference_candidate(reference, sources=sources)

        links = list(await asyncio.gather(*(bounded_link(item) for item in extraction.references)))
        for link in links:
            reference = link.reference
            reports.extend(link.provider_reports)
            if (
                link.status == ReferenceLinkStatus.RESOLVED
                and reference.evidence_level == ReferenceEvidenceLevel.VERIFIED_ANCHOR
            ):
                assertions.append(
                    CitationAssertion(
                        subject_record_id=seed.record_id,
                        object_record_id=link.candidates[0].record_id,
                        relation=RelationKind.REFERENCES,
                        provenance=Provenance(
                            provider=f"fulltext:{extraction.source_format}",
                            source_record_id=reference.reference_id,
                        ),
                        evidence_type=CitationEvidenceType.FULLTEXT_ANCHOR,
                        verification_status=CitationVerificationStatus.VERIFIED,
                        evidence={
                            "method": link.method,
                            "evidence_level": reference.evidence_level.value,
                            "callout_count": reference.callout_count,
                            "citation_contexts": [
                                context.model_dump(mode="json") for context in reference.contexts
                            ],
                        },
                    )
                )

        resolved_count = sum(link.status == ReferenceLinkStatus.RESOLVED for link in links)
        ambiguous_count = sum(link.status == ReferenceLinkStatus.AMBIGUOUS for link in links)
        unresolved_count = len(links) - resolved_count - ambiguous_count
        status = self._overall_status(reports, bool(resolved_count))
        if resolved_count and (ambiguous_count or unresolved_count):
            status = RunStatus.PARTIAL
        all_candidates = [paper for link in links for paper in link.candidates]
        return ReferenceLinkingResult(
            seed=seed,
            extraction=extraction,
            status=status,
            links=links,
            assertions=assertions,
            edges=self._visible_edges(assertions, [seed, *all_candidates]),
            provider_reports=reports,
            resolved_count=resolved_count,
            ambiguous_count=ambiguous_count,
            unresolved_count=unresolved_count,
        )

    async def _link_reference_candidate(
        self,
        reference: ExtractedReference,
        *,
        sources: list[str] | None,
    ) -> ReferenceLink:
        if reference.doi:
            result = await self._resolve(reference.doi, sources=sources)
            candidates = result.papers
            item_reports = result.provider_reports
            method = "doi_resolve"
        elif reference.title:
            # Bibliographies frequently disagree by one year between online and
            # print publication.  Keep that uncertainty explicit while using
            # venue metadata to reduce false-positive title matches.
            year_from = (
                max(1000, reference.publication_year - 1)
                if reference.publication_year is not None
                else None
            )
            year_to = (
                min(3000, reference.publication_year + 1)
                if reference.publication_year is not None
                else None
            )
            result = await self._search(
                SearchQuery(
                    text=reference.title,
                    title=reference.title,
                    year_from=year_from,
                    year_to=year_to,
                    venue=reference.venue,
                    limit=5,
                ),
                sources=sources,
            )
            candidates = result.papers
            item_reports = result.provider_reports
            method = (
                "title_metadata_search"
                if reference.publication_year is not None or reference.venue
                else "title_search"
            )
        else:
            candidates = []
            item_reports = []
            method = "insufficient_metadata"
        candidates, candidate_scores = rank_reference_candidates(
            reference,
            candidates,
            identifier_match=reference.doi is not None,
        )
        status, decision_reason = decide_reference_match(
            candidate_scores,
            policy=self.reference_matching_policy,
            identifier_match=reference.doi is not None,
        )
        return ReferenceLink(
            reference=reference,
            status=status,
            method=method,
            candidates=candidates,
            candidate_scores=candidate_scores,
            decision_reason=decision_reason,
            entity_view=build_entity_view(candidates),
            provider_reports=item_reports,
        )

    async def _related(
        self,
        query: RelatedQuery,
        *,
        sources: list[str] | None,
    ) -> RelatedResult:
        selected = self._select(sources)
        active = [provider for provider in selected if provider.capabilities.related]
        reports = [
            ProviderReport(
                provider=provider.name,
                operation="related",
                status=RunStatus.SKIPPED,
                error_code="capability_none",
                error_message="provider does not expose related-paper retrieval",
            )
            for provider in selected
            if not provider.capabilities.related
        ]
        outputs = await asyncio.gather(
            *(self._call(provider, "related", query) for provider in active)
        )
        raw_papers = [paper for batch, _ in outputs for paper in batch.papers]
        reports.extend(report for _, report in outputs)

        negative_results = await asyncio.gather(
            *(
                self._resolve(identifier, sources=sources)
                for identifier in query.negative_identifiers
            )
        )
        reports.extend(
            report.model_copy(
                update={
                    "context": {
                        **report.context,
                        "negative_identifier": result.identifier,
                    }
                }
            )
            for result in negative_results
            for report in result.provider_reports
        )
        negative_papers = [paper for result in negative_results for paper in result.papers]
        resolution = self._resolve_identities([*raw_papers, *negative_papers])
        negative_ids = {
            resolution.record_id_map.get(paper.record_id, paper.record_id)
            for paper in negative_papers
        }

        evidence_by_id: dict[str, list[RetrievalEvidence]] = {}
        score_by_id: dict[str, float] = {}
        for paper in raw_papers:
            canonical_id = resolution.record_id_map.get(paper.record_id, paper.record_id)
            if canonical_id in negative_ids:
                continue
            for source in paper.source_records:
                if source.retrieval_method is None or source.provider_rank is None:
                    continue
                contribution = 1.0 / (query.rrf_k + source.provider_rank)
                evidence = RetrievalEvidence(
                    provider=source.provider,
                    method=source.retrieval_method,
                    provider_rank=source.provider_rank,
                    provider_score=source.provider_score,
                    rrf_contribution=contribution,
                    context=source.retrieval_context,
                )
                evidence_by_id.setdefault(canonical_id, []).append(evidence)
                score_by_id[canonical_id] = score_by_id.get(canonical_id, 0.0) + contribution

        canonical_by_id = {paper.record_id: paper for paper in resolution.papers}
        ranked_ids = sorted(
            evidence_by_id,
            key=lambda record_id: (-score_by_id[record_id], record_id),
        )
        selected_ids = ranked_ids[: query.limit]
        rankings = [
            RelatedRanking(
                record_id=record_id,
                rank=rank,
                score=score_by_id[record_id],
                evidence=self._unique_models(evidence_by_id[record_id]),
            )
            for rank, record_id in enumerate(selected_ids, start=1)
        ]
        related_reports = [report for report in reports if report.operation == "related"]
        return RelatedResult(
            query=query,
            status=self._overall_status(related_reports, bool(rankings)),
            papers=[canonical_by_id[record_id] for record_id in selected_ids],
            rankings=rankings,
            provider_reports=reports,
            identity_decisions=resolution.decisions,
            raw_record_count=len(raw_papers),
            canonical_record_count=len(evidence_by_id),
            entity_view=build_entity_view(
                [canonical_by_id[record_id] for record_id in selected_ids]
            ),
            truncated=any(batch.truncated for batch, _ in outputs) or len(ranked_ids) > query.limit,
        )

    async def references(
        self, identifier: str, *, limit: int = 100, sources: list[str] | None = None
    ) -> GraphResult:
        identifier = self._validated_identifier(identifier)
        self._validate_relation_limit(limit)
        return await self._audited(
            "references",
            {"identifier": identifier, "limit": limit, "sources": sources},
            lambda: self._relations(identifier, "references", limit=limit, sources=sources),
        )

    async def citations(
        self, identifier: str, *, limit: int = 100, sources: list[str] | None = None
    ) -> GraphResult:
        identifier = self._validated_identifier(identifier)
        self._validate_relation_limit(limit)
        return await self._audited(
            "citations",
            {"identifier": identifier, "limit": limit, "sources": sources},
            lambda: self._relations(identifier, "citations", limit=limit, sources=sources),
        )

    async def expand(
        self,
        query: GraphExpansionQuery,
        *,
        sources: list[str] | None = None,
    ) -> GraphExpansionResult:
        return await self._audited(
            "expand",
            {"query": query.model_dump(mode="json"), "sources": sources},
            lambda: self._expand(query, sources=sources),
        )

    async def _audited(
        self,
        operation: str,
        request: dict,
        action: Callable[[], Awaitable[ResultT]],
    ) -> ResultT:
        run_id = self.store.begin_run(operation, request) if self.store else None
        token = current_run_id.set(run_id) if run_id else None
        try:
            result = await action()
            # Compute after aggregation but exclude the fingerprint field itself
            # and retrieval timestamps (see fingerprint.py).
            result.fingerprint = stable_fingerprint(result)
            if self.store:
                assertions = getattr(result, "assertions", None)
                edges = getattr(result, "edges", None)
                if isinstance(assertions, list) and isinstance(edges, list):
                    # Persist the evidence graph in the same audited success path.
                    # A storage failure therefore fails the run instead of silently
                    # returning a result that autoresearch cannot replay later.
                    self.store.record_citation_evidence(assertions, edges)
        except Exception as exc:
            if self.store and run_id:
                self.store.finish_run(
                    run_id,
                    status="failed",
                    result={"error_type": type(exc).__name__},
                )
            raise
        else:
            if self.store and run_id:
                self.store.finish_run(
                    run_id,
                    status=str(result.status),
                    result=result.model_dump(mode="json"),
                    fingerprint=result.fingerprint,
                )
            return result
        finally:
            if token is not None:
                current_run_id.reset(token)

    async def _relations(
        self,
        identifier: str,
        operation: str,
        *,
        limit: int,
        sources: list[str] | None,
    ) -> GraphResult:
        selected = self._select(sources)
        seed_outputs = await asyncio.gather(
            *(self._call(provider, "resolve", identifier) for provider in selected)
        )
        seed_resolution = self._resolve_identities(
            [paper for batch, _ in seed_outputs for paper in batch.papers]
        )
        seeds = seed_resolution.papers
        if not seeds:
            outcomes = [
                f"{report.provider}={report.error_code or report.status.value}"
                for _, report in seed_outputs
            ]
            raise LookupError(
                f"paper not found: {identifier}; provider outcomes: {', '.join(outcomes) or 'none'}"
            )
        seed = seeds[0]

        # Provider-native IDs are not portable. If one source resolves an
        # OpenAlex/S2/etc. ID, translate it through the canonical seed's DOI,
        # arXiv ID, PMID, or another claimed identifier before declaring other
        # sources unavailable. Runtime failures and throttles are not retried
        # under a different identifier because that would amplify an outage.
        traversal_identifiers = {provider.name: identifier for provider in selected}
        portable_identifier = self._preferred_identifier(seed)
        fallback_indexes = [
            index
            for index, (provider, (batch, report)) in enumerate(
                zip(selected, seed_outputs, strict=True)
            )
            if getattr(provider.capabilities, operation) == "list"
            and not batch.papers
            and report.status == RunStatus.EMPTY
            and portable_identifier.casefold() != identifier.casefold()
        ]
        fallback_outputs = await asyncio.gather(
            *(
                self._call(selected[index], "resolve", portable_identifier)
                for index in fallback_indexes
            )
        )
        seed_outputs = list(seed_outputs)
        for index, fallback_output in zip(fallback_indexes, fallback_outputs, strict=True):
            batch, report = fallback_output
            traversal_identifiers[selected[index].name] = portable_identifier
            seed_outputs[index] = (
                batch,
                report.model_copy(
                    update={
                        "context": {
                            **report.context,
                            "identifier_translated_from": identifier,
                            "identifier_used": portable_identifier,
                        }
                    }
                ),
            )

        # Metadata-aware adapters can recover from a truly empty ID lookup.
        # Do not amplify throttles or authentication failures with new searches.
        for index, (provider, (batch, report)) in enumerate(
            zip(selected, seed_outputs, strict=True)
        ):
            if (
                not batch.papers
                and report.status == RunStatus.EMPTY
                and getattr(provider.capabilities, operation) == "list"
            ):
                recovered, recovery_report = await self._call(provider, "resolve_seed", seed)
                if recovered.papers or recovery_report.status != RunStatus.EMPTY:
                    seed_outputs[index] = (recovered, recovery_report)

        # Include fallback seed records in the canonical seed and alias map.
        seed_resolution = self._resolve_identities(
            [paper for batch, _ in seed_outputs for paper in batch.papers]
        )
        seed = seed_resolution.papers[0]
        available: list[tuple[ScholarlyProvider, Paper, str]] = []
        relation_reports: list[ProviderReport] = []
        for provider, (batch, seed_report) in zip(selected, seed_outputs, strict=True):
            capability = getattr(provider.capabilities, operation)
            # A citation count cannot be traversed like a citation list. Known
            # capability gaps are SKIPPED; runtime failures remain FAILED/THROTTLED.
            if capability != "list":
                relation_reports.append(
                    ProviderReport(
                        provider=provider.name,
                        operation=operation,
                        status=RunStatus.SKIPPED,
                        error_code=f"capability_{capability}",
                        error_message=(
                            f"{provider.name} exposes {operation} as {capability}, "
                            "not as a traversable list"
                        ),
                    )
                )
            elif not batch.papers:
                if seed_report.status in {RunStatus.FAILED, RunStatus.THROTTLED}:
                    # A runtime failure while resolving this provider's seed is
                    # still a failed relation branch, not a capability skip.
                    relation_reports.append(
                        seed_report.model_copy(
                            update={
                                "operation": operation,
                                "context": {
                                    **seed_report.context,
                                    "failed_stage": "seed_resolve",
                                },
                            }
                        )
                    )
                else:
                    relation_reports.append(
                        ProviderReport(
                            provider=provider.name,
                            operation=operation,
                            status=RunStatus.SKIPPED,
                            error_code="seed_not_resolved",
                            error_message="provider could not resolve the seed identifier",
                        )
                    )
            else:
                available.append(
                    (
                        provider,
                        batch.papers[0],
                        provider.traversal_identifier(
                            batch.papers[0], traversal_identifiers[provider.name]
                        ),
                    )
                )
        outputs = await asyncio.gather(
            *(
                self._call(provider, operation, traversal_identifier, limit=limit)
                for provider, _, traversal_identifier in available
            )
        )
        raw_papers: list[Paper] = []
        reports: list[ProviderReport] = [report for _, report in seed_outputs]
        reports.extend(relation_reports)
        assertions: list[CitationAssertion] = []
        unresolved_references: list[UnresolvedReference] = []
        raw_ids_by_provider: dict[str, list[str]] = {}
        truncated = False
        for (provider, provider_seed, _), (batch, report) in zip(available, outputs, strict=True):
            raw_papers.extend(batch.papers)
            raw_ids_by_provider[provider.name] = [paper.record_id for paper in batch.papers]
            reports.append(report)
            relation_reports.append(report)
            unresolved_references.extend(batch.unresolved_references)
            truncated |= batch.truncated
            for paper in batch.papers:
                source_record = paper.source_records[0] if paper.source_records else None
                if operation == "references":
                    # REFERENCES is seed -> older cited work.
                    subject, object_ = provider_seed.record_id, paper.record_id
                    relation = RelationKind.REFERENCES
                else:
                    # CITES is later citing work -> seed. This explicit direction
                    # avoids forward/backward-search terminology ambiguity.
                    subject, object_ = paper.record_id, provider_seed.record_id
                    relation = RelationKind.CITES
                assertions.append(
                    CitationAssertion(
                        subject_record_id=subject,
                        object_record_id=object_,
                        relation=relation,
                        provenance=Provenance(
                            provider=provider.name,
                            source_record_id=(
                                source_record.source_record_id if source_record else paper.record_id
                            ),
                            source_url=source_record.source_url if source_record else None,
                        ),
                        evidence_type=(
                            CitationEvidenceType.DEPOSITOR_METADATA
                            if provider.name == "crossref" and operation == "references"
                            else CitationEvidenceType.PROVIDER_GRAPH
                        ),
                        verification_status=CitationVerificationStatus.PROVIDER_ASSERTED,
                        evidence={
                            "method": "provider_graph",
                            "source_record_fallback": source_record is None,
                        },
                    )
                )
        resolution = self._resolve_identities(raw_papers)
        papers = resolution.papers
        edges = self._visible_edges(assertions, [seed, *papers])
        entity_view = build_entity_view([seed, *papers])
        return GraphResult(
            operation=operation,
            seed=seed,
            status=self._overall_status(relation_reports, bool(papers or unresolved_references)),
            papers=papers,
            assertions=assertions,
            edges=edges,
            unresolved_references=unresolved_references,
            identity_decisions=seed_resolution.decisions + resolution.decisions,
            entity_view=entity_view,
            provider_reports=reports,
            provider_contributions=self._provider_contributions(
                raw_ids_by_provider,
                resolution.record_id_map,
                len(papers),
            ),
            provider_overlaps=self._provider_overlaps(
                raw_ids_by_provider,
                resolution.record_id_map,
            ),
            raw_record_count=len(raw_papers),
            manifestation_count=len(entity_view.manifestations),
            work_family_count=len(entity_view.work_families),
            truncated=truncated,
        )

    async def _expand(
        self,
        query: GraphExpansionQuery,
        *,
        sources: list[str] | None,
    ) -> GraphExpansionResult:
        expansion_started = self._clock()
        requested_identifiers = query.seed_identifiers
        checkpoint_key = make_checkpoint_key(
            "aggregate",
            "expand",
            stable_fingerprint(
                {
                    "query": query.model_dump(mode="json"),
                    "sources": sorted(sources or self.providers),
                    "identity_events": [
                        event.model_dump(mode="json")
                        for event in (
                            self.store.list_identity_events(active_only=True) if self.store else []
                        )
                    ],
                }
            ),
            query.top_k,
        )
        checkpoint = self.store.load_checkpoint(checkpoint_key) if self.store else None
        state = checkpoint.state if checkpoint else None
        if state and state.get("version") == 1:
            # A layer checkpoint is self-contained. Provider pagination tokens
            # remain internal to relation calls and cannot leak into graph state.
            seeds = [Paper.model_validate(item) for item in state["seeds"]]
            seed_ids = set(state["seed_ids"])
            all_papers = [Paper.model_validate(item) for item in state["all_papers"]]
            frontier = [Paper.model_validate(item) for item in state["frontier"]]
            accepted_ids = set(state["accepted_ids"])
            discovered_ids = set(state["discovered_ids"])
            assertions = [CitationAssertion.model_validate(item) for item in state["assertions"]]
            reports = [ProviderReport.model_validate(item) for item in state["reports"]]
            unresolved = [UnresolvedReference.model_validate(item) for item in state["unresolved"]]
            decisions = [IdentityDecision.model_validate(item) for item in state["decisions"]]
            paths = [DiscoveryPath.model_validate(item) for item in state["paths"]]
            origins_by_node = {key: set(values) for key, values in state["origins_by_node"].items()}
            raw_record_count = int(state["raw_record_count"])
            filtered_candidate_ids = set(state["filtered_candidate_ids"])
            filter_exclusions = [
                GraphFilterExclusion.model_validate(item) for item in state["filter_exclusions"]
            ]
            expanded_node_count = int(state["expanded_node_count"])
            truncation_reasons = list(state["truncation_reasons"])
            missing_seed_identifiers = list(state["missing_seed_identifiers"])
            start_depth = int(state["next_depth"])
        else:
            if checkpoint and self.store:
                self.store.delete_checkpoint(checkpoint_key)
            seed_results = await asyncio.gather(
                *(
                    self._resolve(identifier, sources=sources)
                    for identifier in requested_identifiers
                )
            )
            missing_seed_identifiers = [
                identifier
                for identifier, result in zip(requested_identifiers, seed_results, strict=True)
                if not result.papers
            ]
            raw_seeds = [paper for result in seed_results for paper in result.papers]
            if not raw_seeds:
                outcomes = [
                    (f"{identifier}[{report.provider}]={report.error_code or report.status.value}")
                    for identifier, result in zip(requested_identifiers, seed_results, strict=True)
                    for report in result.provider_reports
                ]
                raise LookupError(
                    "no seed papers resolved for "
                    f"{', '.join(missing_seed_identifiers)}; provider outcomes: "
                    f"{', '.join(outcomes) or 'none'}"
                )
            seed_resolution = self._resolve_identities(raw_seeds)
            seeds = seed_resolution.papers
            seed_ids = {paper.record_id for paper in seeds}
            all_papers = list(seeds)
            frontier = list(seeds)
            accepted_ids = set(seed_ids)
            discovered_ids: set[str] = set()
            assertions: list[CitationAssertion] = []
            reports = [
                report.model_copy(
                    update={
                        "context": {
                            **report.context,
                            "seed_identifier": identifier,
                        }
                    }
                )
                for identifier, result in zip(requested_identifiers, seed_results, strict=True)
                for report in result.provider_reports
            ]
            reports.extend(
                ProviderReport(
                    provider="aggregate",
                    operation="expand_seed",
                    status=RunStatus.FAILED,
                    error_code="seed_not_resolved",
                    error_message="seed identifier could not be resolved",
                    context={"seed_identifier": identifier},
                )
                for identifier in missing_seed_identifiers
            )
            unresolved: list[UnresolvedReference] = []
            decisions = [
                decision for result in seed_results for decision in result.identity_decisions
            ]
            decisions.extend(seed_resolution.decisions)
            paths = [
                DiscoveryPath(
                    seed_record_id=seed_id,
                    target_record_id=seed_id,
                    depth=0,
                    operation="seed",
                    discovery_order=order,
                )
                for order, seed_id in enumerate(sorted(seed_ids))
            ]
            origins_by_node = {seed_id: {seed_id} for seed_id in seed_ids}
            raw_record_count = 0
            filtered_candidate_ids: set[str] = set()
            filter_exclusions: list[GraphFilterExclusion] = []
            expanded_node_count = 0
            truncation_reasons: list[str] = []
            start_depth = 1

        operations = {
            ExpansionDirection.REFERENCES: ("references",),
            ExpansionDirection.CITATIONS: ("citations",),
            ExpansionDirection.BOTH: ("references", "citations"),
        }[query.direction]

        def save_graph_checkpoint(next_depth: int) -> None:
            if not self.store:
                return
            # Commit only complete BFS layers. A crash can therefore repeat at
            # most one layer and never resume from a half-accepted frontier.
            self.store.save_checkpoint(
                checkpoint_key,
                provider="aggregate",
                operation="expand",
                cursor=str(next_depth),
                state={
                    "version": 1,
                    "next_depth": next_depth,
                    "seeds": [item.model_dump(mode="json") for item in seeds],
                    "seed_ids": sorted(seed_ids),
                    "all_papers": [item.model_dump(mode="json") for item in all_papers],
                    "frontier": [item.model_dump(mode="json") for item in frontier],
                    "accepted_ids": sorted(accepted_ids),
                    "discovered_ids": sorted(discovered_ids),
                    "assertions": [item.model_dump(mode="json") for item in assertions],
                    "reports": [item.model_dump(mode="json") for item in reports],
                    "unresolved": [item.model_dump(mode="json") for item in unresolved],
                    "decisions": [item.model_dump(mode="json") for item in decisions],
                    "paths": [item.model_dump(mode="json") for item in paths],
                    "origins_by_node": {
                        key: sorted(values) for key, values in origins_by_node.items()
                    },
                    "raw_record_count": raw_record_count,
                    "filtered_candidate_ids": sorted(filtered_candidate_ids),
                    "filter_exclusions": [
                        item.model_dump(mode="json") for item in filter_exclusions
                    ],
                    "expanded_node_count": expanded_node_count,
                    "truncation_reasons": truncation_reasons,
                    "missing_seed_identifiers": missing_seed_identifiers,
                },
            )

        for depth in range(start_depth, query.depth + 1):
            if (
                query.max_runtime_seconds is not None
                and self._clock() - expansion_started >= query.max_runtime_seconds
            ):
                self._append_once(truncation_reasons, "runtime_budget")
                break
            if not frontier:
                break
            expandable = frontier[: query.frontier_cap]
            if len(frontier) > len(expandable):
                self._append_once(truncation_reasons, "frontier_cap")
            expanded_node_count += len(expandable)
            scheduled = [(paper, operation) for paper in expandable for operation in operations]
            outputs = await asyncio.gather(
                *(
                    self._relations(
                        self._preferred_identifier(paper),
                        operation,
                        limit=query.per_node_limit,
                        sources=sources,
                    )
                    for paper, operation in scheduled
                ),
                return_exceptions=True,
            )

            candidate_entries: list[tuple[Paper, str, str, bool, dict[str, str]]] = []
            for (parent, operation), output in zip(scheduled, outputs, strict=True):
                if isinstance(output, Exception):
                    # A seed disappearing between resolve and traversal is a
                    # diagnosable partial branch, not a reason to lose other nodes.
                    reports.append(
                        ProviderReport(
                            provider="aggregate",
                            operation=operation,
                            status=RunStatus.FAILED,
                            error_code=type(output).__name__,
                            error_message="graph branch failed before producing a result",
                            context={"node": parent.record_id, "depth": depth},
                        )
                    )
                    continue
                assertions.extend(output.assertions)
                unresolved.extend(output.unresolved_references)
                decisions.extend(output.identity_decisions)
                raw_record_count += sum(
                    report.retrieved_count
                    for report in output.provider_reports
                    if report.operation == operation
                )
                if output.truncated:
                    self._append_once(truncation_reasons, "provider_limit")
                reports.extend(
                    report.model_copy(
                        update={
                            "context": {
                                **report.context,
                                "node": parent.record_id,
                                "depth": depth,
                            }
                        }
                    )
                    for report in output.provider_reports
                )
                continuations = {
                    report.provider: report.next_cursor
                    for report in output.provider_reports
                    if report.operation == operation and report.next_cursor is not None
                }
                candidate_entries.extend(
                    (paper, parent.record_id, operation, output.truncated, continuations)
                    for paper in output.papers
                )

            if not candidate_entries:
                frontier = []
                save_graph_checkpoint(depth + 1)
                continue

            layer_resolution = self._resolve_identities(
                [*all_papers, *(paper for paper, _, _, _, _ in candidate_entries)]
            )
            decisions.extend(layer_resolution.decisions)
            canonical_by_id = {paper.record_id: paper for paper in layer_resolution.papers}
            next_frontier: list[Paper] = []
            next_ids: set[str] = set()
            for paper, predecessor, operation, branch_truncated, continuations in candidate_entries:
                canonical_id = layer_resolution.record_id_map.get(paper.record_id, paper.record_id)
                canonical_predecessor = layer_resolution.record_id_map.get(predecessor, predecessor)
                if canonical_id not in accepted_ids:
                    filter_reasons = self._candidate_filter_reasons(
                        canonical_by_id[canonical_id], query
                    )
                    if filter_reasons:
                        filtered_candidate_ids.add(canonical_id)
                        filter_exclusions.append(
                            GraphFilterExclusion(
                                record_id=canonical_id,
                                predecessor_record_id=canonical_predecessor,
                                depth=depth,
                                operation=operation,
                                reasons=filter_reasons,
                            )
                        )
                        continue
                    if len(discovered_ids) >= query.top_k:
                        self._append_once(truncation_reasons, "top_k")
                        continue
                    accepted_ids.add(canonical_id)
                    discovered_ids.add(canonical_id)
                    if canonical_id not in next_ids:
                        next_ids.add(canonical_id)
                        next_frontier.append(canonical_by_id[canonical_id])
                origins = origins_by_node.get(canonical_predecessor, {canonical_predecessor})
                origins_by_node.setdefault(canonical_id, set()).update(origins)
                for origin in sorted(origins):
                    paths.append(
                        DiscoveryPath(
                            seed_record_id=origin,
                            target_record_id=canonical_id,
                            predecessor_record_id=canonical_predecessor,
                            depth=depth,
                            operation=operation,
                            discovery_order=len(paths),
                            # No relevance model is calibrated yet. Explicit
                            # nulls prevent discovery order from masquerading
                            # as a relevance score.
                            score=None,
                            score_method=None,
                            branch_truncated=branch_truncated,
                            continuations=continuations,
                        )
                    )

            all_papers = [
                paper for paper in layer_resolution.papers if paper.record_id in accepted_ids
            ]
            frontier = next_frontier
            save_graph_checkpoint(depth + 1)

        final_resolution = self._resolve_identities(all_papers)
        decisions.extend(final_resolution.decisions)
        final_papers = final_resolution.papers
        allowed = {paper.record_id for paper in final_papers}
        aliases = self._record_aliases(final_papers)
        visible_assertions = [
            assertion
            for assertion in assertions
            if aliases.get(assertion.subject_record_id, assertion.subject_record_id) in allowed
            and aliases.get(assertion.object_record_id, assertion.object_record_id) in allowed
        ]
        edges = self._visible_edges(visible_assertions, final_papers)
        co_citations = self._graph_similarities(edges, GraphSimilarityKind.CO_CITATION)
        bibliographic_couplings = self._graph_similarities(
            edges, GraphSimilarityKind.BIBLIOGRAPHIC_COUPLING
        )
        rankings = self._pagerank(final_papers, edges)
        relation_reports = [
            report for report in reports if report.operation in {"references", "citations"}
        ]
        status = self._overall_status(relation_reports, bool(discovered_ids))
        if "runtime_budget" in truncation_reasons and not relation_reports:
            status = RunStatus.PARTIAL
        if missing_seed_identifiers:
            # At least one seed succeeded (otherwise we raised above), so the
            # useful graph is returned while the missing branch remains visible.
            status = RunStatus.PARTIAL
        depth_reached = max((path.depth for path in paths), default=0)
        entity_view = build_entity_view(final_papers)
        result = GraphExpansionResult(
            query=query,
            seeds=[paper for paper in final_papers if paper.record_id in seed_ids],
            missing_seed_identifiers=missing_seed_identifiers,
            status=status,
            papers=final_papers,
            edges=edges,
            co_citations=co_citations,
            bibliographic_couplings=bibliographic_couplings,
            rankings=rankings,
            filter_exclusions=self._unique_models(filter_exclusions),
            filtered_candidate_count=len(filtered_candidate_ids),
            assertions=visible_assertions,
            discovery_paths=self._unique_models(paths),
            provider_reports=reports,
            unresolved_references=unresolved,
            identity_decisions=self._unique_models(decisions),
            raw_record_count=raw_record_count,
            manifestation_count=len(entity_view.manifestations),
            work_family_count=len(entity_view.work_families),
            entity_view=entity_view,
            expanded_node_count=expanded_node_count,
            depth_reached=depth_reached,
            truncated=bool(truncation_reasons),
            truncation_reasons=truncation_reasons,
        )
        if self.store:
            self.store.delete_checkpoint(checkpoint_key)
        return result

    async def _call(self, provider: ScholarlyProvider, operation: str, *args, **kwargs):
        try:
            value = await getattr(provider, operation)(*args, **kwargs)
            if isinstance(value, ProviderBatch):
                batch = value
            elif value is None:
                batch = ProviderBatch()
            else:
                batch = ProviderBatch(papers=[value])
            status = batch.status or (
                RunStatus.COMPLETE
                if batch.papers or batch.unresolved_references
                else RunStatus.EMPTY
            )
            return batch, ProviderReport(
                provider=provider.name,
                operation=operation,
                status=status,
                retrieved_count=len(batch.papers),
                unresolved_count=len(batch.unresolved_references),
                total_available=batch.total_available,
                truncated=batch.truncated,
                next_cursor=batch.next_cursor,
                filter_execution=batch.filter_execution,
                context=batch.context,
            )
        except httpx.HTTPStatusError as exc:
            status = RunStatus.THROTTLED if exc.response.status_code == 429 else RunStatus.FAILED
            reliability = exc.response.extensions.get("scholarly_reliability", {})
            message = (
                f"provider returned HTTP {exc.response.status_code}; "
                "request URL omitted to protect credentials"
            )
            # Providers may explain a throttle that no retry policy can fix,
            # such as anonymous traffic sharing one global quota pool.
            hint = (
                getattr(provider, "throttle_hint", None) if status == RunStatus.THROTTLED else None
            )
            if hint:
                message = f"{message}; {hint}"
            return ProviderBatch(), ProviderReport(
                provider=provider.name,
                operation=operation,
                status=status,
                error_code=f"http_{exc.response.status_code}",
                error_message=message,
                context={
                    key: reliability[key]
                    for key in ("attempt_count", "retry_delays")
                    if key in reliability
                },
            )
        except httpx.HTTPError as exc:
            return ProviderBatch(), ProviderReport(
                provider=provider.name,
                operation=operation,
                status=RunStatus.FAILED,
                error_code=type(exc).__name__,
                error_message="provider request failed; URL omitted to protect credentials",
            )
        except ProviderOperationError as exc:
            return ProviderBatch(), ProviderReport(
                provider=provider.name,
                operation=operation,
                status=RunStatus.FAILED,
                error_code=exc.code,
                error_message=str(exc),
            )
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            # Provider payload shapes can drift independently. Mapping failures
            # stay isolated to that branch so a healthy source can still return
            # a PARTIAL result with a concrete adapter-level error code.
            return ProviderBatch(), ProviderReport(
                provider=provider.name,
                operation=operation,
                status=RunStatus.FAILED,
                error_code=type(exc).__name__,
                error_message=str(exc),
            )

    def _select(self, sources: list[str] | None) -> list[ScholarlyProvider]:
        names = sources or list(self.providers)
        unknown = sorted(set(names) - self.providers.keys())
        if unknown:
            raise ValueError(f"unknown providers: {', '.join(unknown)}")
        return [self.providers[name] for name in names]

    @staticmethod
    def _preferred_identifier(paper: Paper) -> str:
        """Choose the most portable traversal ID without inventing a title lookup."""

        prefixes = {
            IdentifierScheme.DOI: "",
            IdentifierScheme.ARXIV: "arxiv:",
            IdentifierScheme.PMID: "PMID:",
            IdentifierScheme.PMCID: "PMCID:",
            IdentifierScheme.OPENALEX: "",
            IdentifierScheme.SEMANTIC_SCHOLAR: "",
            IdentifierScheme.CROSSREF: "",
            IdentifierScheme.GOOGLE_SCHOLAR: "google_scholar:",
            IdentifierScheme.DBLP: "dblp:",
            IdentifierScheme.OMID: "omid:",
            IdentifierScheme.OPENAIRE: "openaire:",
            IdentifierScheme.INSPIRE: "inspire:",
            IdentifierScheme.OPENREVIEW: "openreview:",
            IdentifierScheme.ACL_ANTHOLOGY: "acl_anthology:",
        }
        for scheme in (
            IdentifierScheme.DOI,
            IdentifierScheme.ARXIV,
            IdentifierScheme.PMID,
            IdentifierScheme.PMCID,
            IdentifierScheme.OMID,
            IdentifierScheme.DBLP,
            IdentifierScheme.INSPIRE,
            IdentifierScheme.OPENREVIEW,
            IdentifierScheme.ACL_ANTHOLOGY,
            IdentifierScheme.OPENAIRE,
            IdentifierScheme.OPENALEX,
            IdentifierScheme.SEMANTIC_SCHOLAR,
            IdentifierScheme.CROSSREF,
            IdentifierScheme.GOOGLE_SCHOLAR,
        ):
            claim = next((item for item in paper.identifiers if item.scheme == scheme), None)
            if claim:
                return f"{prefixes[scheme]}{claim.value}"
        # Provider-native records in fixtures and sparse APIs remain traversable
        # by their source ID; a title is deliberately never used as identity.
        return paper.record_id.split(":", 1)[-1]

    @staticmethod
    def _record_aliases(papers: list[Paper]) -> dict[str, str]:
        aliases: dict[str, str] = {}
        for paper in papers:
            aliases[paper.record_id] = paper.record_id
            for source in paper.source_records:
                aliases[f"{source.provider}:{source.source_record_id}"] = paper.record_id
        return aliases

    @classmethod
    def _visible_edges(
        cls,
        assertions: list[CitationAssertion],
        papers: list[Paper],
    ) -> list[VisibleCitationEdge]:
        """Fold canonical edges while retaining all provider-level evidence."""

        aliases = cls._record_aliases(papers)
        grouped: dict[tuple[str, str], list[CitationAssertion]] = {}
        for assertion in assertions:
            citing = aliases.get(assertion.subject_record_id, assertion.subject_record_id)
            cited = aliases.get(assertion.object_record_id, assertion.object_record_id)
            # Never materialize a self-loop. This is a final provider-neutral
            # guard against upstream graph anomalies and identity aliases that
            # collapse both endpoints onto the same canonical work.
            if citing == cited:
                continue
            grouped.setdefault((citing, cited), []).append(assertion)
        return [
            VisibleCitationEdge(
                citing_record_id=citing,
                cited_record_id=cited,
                assertions=cls._unique_models(edge_assertions),
            )
            for (citing, cited), edge_assertions in grouped.items()
        ]

    @staticmethod
    def _graph_similarities(
        edges: list[VisibleCitationEdge],
        kind: GraphSimilarityKind,
    ) -> list[GraphSimilarity]:
        """Derive explainable pair similarities without using citation counts.

        Co-citation pairs cited works that share a citing work. Bibliographic
        coupling pairs citing works that share a cited work. Only canonical,
        evidence-backed visible edges participate in either calculation.
        """

        neighbors_by_shared_record: dict[str, set[str]] = {}
        for edge in edges:
            if kind == GraphSimilarityKind.CO_CITATION:
                shared_record = edge.citing_record_id
                neighbor = edge.cited_record_id
            else:
                shared_record = edge.cited_record_id
                neighbor = edge.citing_record_id
            neighbors_by_shared_record.setdefault(shared_record, set()).add(neighbor)

        shared_by_pair: dict[tuple[str, str], set[str]] = {}
        for shared_record, neighbors in neighbors_by_shared_record.items():
            for pair in combinations(sorted(neighbors), 2):
                shared_by_pair.setdefault(pair, set()).add(shared_record)

        return [
            GraphSimilarity(
                kind=kind,
                left_record_id=left,
                right_record_id=right,
                shared_count=len(shared_records),
                shared_record_ids=sorted(shared_records),
            )
            for (left, right), shared_records in sorted(shared_by_pair.items())
        ]

    @staticmethod
    def _candidate_filter_reasons(
        paper: Paper,
        query: GraphExpansionQuery,
    ) -> list[GraphFilterReason]:
        """Evaluate candidate-only filters after layer-wide identity enrichment."""

        reasons: list[GraphFilterReason] = []
        if query.year_from is not None or query.year_to is not None:
            if paper.publication_year is None:
                reasons.append(GraphFilterReason.PUBLICATION_YEAR_MISSING)
            elif query.year_from is not None and paper.publication_year < query.year_from:
                reasons.append(GraphFilterReason.YEAR_BEFORE_RANGE)
            elif query.year_to is not None and paper.publication_year > query.year_to:
                reasons.append(GraphFilterReason.YEAR_AFTER_RANGE)

        if query.include_work_types:
            if paper.work_type is None:
                reasons.append(GraphFilterReason.WORK_TYPE_MISSING)
            elif " ".join(paper.work_type.split()).casefold() not in query.include_work_types:
                reasons.append(GraphFilterReason.WORK_TYPE_NOT_INCLUDED)
        return reasons

    @staticmethod
    def _pagerank(
        papers: list[Paper],
        edges: list[VisibleCitationEdge],
        *,
        damping: float = 0.85,
        iterations: int = 50,
    ) -> list[GraphNodeRanking]:
        """Compute deterministic PageRank over the returned bounded graph only."""

        node_ids = sorted(paper.record_id for paper in papers)
        if not node_ids:
            return []
        node_set = set(node_ids)
        outgoing = {record_id: set() for record_id in node_ids}
        incoming = {record_id: set() for record_id in node_ids}
        for edge in edges:
            if edge.citing_record_id in node_set and edge.cited_record_id in node_set:
                outgoing[edge.citing_record_id].add(edge.cited_record_id)
                incoming[edge.cited_record_id].add(edge.citing_record_id)

        count = len(node_ids)
        scores = {record_id: 1.0 / count for record_id in node_ids}
        for _ in range(iterations):
            dangling = sum(scores[record_id] for record_id in node_ids if not outgoing[record_id])
            updated = {}
            for record_id in node_ids:
                linked = sum(
                    scores[parent] / len(outgoing[parent]) for parent in incoming[record_id]
                )
                updated[record_id] = (
                    (1.0 - damping) / count + damping * linked + damping * dangling / count
                )
            scores = updated

        ranked_ids = sorted(node_ids, key=lambda item: (-scores[item], item))
        return [
            GraphNodeRanking(
                record_id=record_id,
                rank=rank,
                score=scores[record_id],
                components={
                    "incoming_edge_count": float(len(incoming[record_id])),
                    "outgoing_edge_count": float(len(outgoing[record_id])),
                    "damping": damping,
                    "iterations": float(iterations),
                },
            )
            for rank, record_id in enumerate(ranked_ids, start=1)
        ]

    @staticmethod
    def _matches_search_filters(paper: Paper, query: SearchQuery) -> bool:
        """Apply the neutral advanced-search contract to the canonical record."""

        def contains(value: str | None, wanted: str | None) -> bool:
            return wanted is None or (value is not None and wanted.casefold() in value.casefold())

        if not contains(paper.title, query.title):
            return False
        if query.year_from is not None and (
            paper.publication_year is None or paper.publication_year < query.year_from
        ):
            return False
        if query.year_to is not None and (
            paper.publication_year is None or paper.publication_year > query.year_to
        ):
            return False
        if query.author is not None and not any(
            query.author.casefold() in author.name.casefold() for author in paper.authors
        ):
            return False
        if query.open_access is not None and paper.open_access is not query.open_access:
            return False
        if query.expression is not None and not matches_expression(paper, query.expression):
            return False
        if not contains(paper.abstract, query.abstract):
            return False
        if not contains(paper.venue, query.venue):
            return False
        if query.field is not None and not any(
            query.field.casefold() in value.casefold() for value in paper.fields_of_study
        ):
            return False
        if query.work_types and (
            paper.work_type is None
            or " ".join(paper.work_type.split()).casefold() not in query.work_types
        ):
            return False
        if (
            query.min_citations is not None
            and max((claim.count for claim in paper.citation_counts), default=0)
            < query.min_citations
        ):
            return False
        return True

    @staticmethod
    def _sort_search_results(
        papers: list[Paper], sort: SearchSort, *, query: SearchQuery | None = None
    ) -> list[Paper]:
        if sort == SearchSort.RELEVANCE:
            if query is None:
                return papers
            return ScholarService._rank_search_relevance(papers, query.text)
        if sort == SearchSort.NEWEST:
            return sorted(
                papers,
                key=lambda paper: (
                    paper.publication_year is None,
                    -(paper.publication_year or 0),
                    paper.record_id,
                ),
            )
        if sort == SearchSort.OLDEST:
            return sorted(
                papers,
                key=lambda paper: (
                    paper.publication_year is None,
                    paper.publication_year or 0,
                    paper.record_id,
                ),
            )
        return sorted(
            papers,
            key=lambda paper: (
                -max((claim.count for claim in paper.citation_counts), default=0),
                paper.record_id,
            ),
        )

    @staticmethod
    def _rank_search_relevance(papers: list[Paper], query_text: str) -> list[Paper]:
        """Fuse provider ranks with soft lexical coverage, without hard filtering.

        The lexical signal removes obvious off-topic tail records, while RRF
        rewards independent source agreement. No candidate is discarded for a
        missing query term, which preserves the recall-oriented search contract.
        """

        query_tokens = ScholarService._search_tokens(query_text)
        if not query_tokens:
            return papers
        documents = {
            paper.record_id: ScholarService._search_tokens(
                " ".join(
                    value
                    for value in [
                        paper.title,
                        paper.abstract or "",
                        paper.venue or "",
                        " ".join(paper.fields_of_study),
                    ]
                    if value
                )
            )
            for paper in papers
        }
        document_count = max(1, len(papers))
        document_frequency = {
            token: sum(token in tokens for tokens in documents.values()) for token in query_tokens
        }
        weights = {
            token: math.log((document_count + 1) / (document_frequency[token] + 1)) + 1.0
            for token in query_tokens
        }
        total_weight = sum(weights.values()) or 1.0
        normalized_query = " ".join(query_text.casefold().split())

        def score(paper: Paper) -> tuple[float, int, str]:
            title_tokens = ScholarService._search_tokens(paper.title)
            body_tokens = documents[paper.record_id]
            title_coverage = (
                sum(weights[token] for token in query_tokens if token in title_tokens)
                / total_weight
            )
            body_coverage = (
                sum(weights[token] for token in query_tokens if token in body_tokens) / total_weight
            )
            searchable_text = " ".join(
                [paper.title, paper.abstract or "", paper.venue or ""]
            ).casefold()
            phrase_hit = float(normalized_query in " ".join(searchable_text.split()))
            ranks_by_provider: dict[str, int] = {}
            for source in paper.source_records:
                if source.provider_rank is None:
                    continue
                ranks_by_provider[source.provider] = min(
                    ranks_by_provider.get(source.provider, source.provider_rank),
                    source.provider_rank,
                )
            rrf = sum(1.0 / (SEARCH_RRF_K + rank) for rank in ranks_by_provider.values())
            best_rank = min(ranks_by_provider.values(), default=10_000)
            fused = (
                4.0 * title_coverage
                + 1.25 * body_coverage
                + 0.75 * phrase_hit
                + 10.0 * rrf
                + 0.05 * min(len(ranks_by_provider), 3)
            )
            return fused, -best_rank, paper.record_id

        return sorted(papers, key=score, reverse=True)

    @staticmethod
    def _search_tokens(value: str) -> set[str]:
        tokens = {token.casefold() for token in re.findall(r"[^\W_]+", value)}
        meaningful = tokens - SEARCH_STOP_WORDS
        return meaningful or tokens

    @staticmethod
    def _provider_contributions(
        raw_ids_by_provider: dict[str, list[str]],
        record_id_map: dict[str, str],
        canonical_total: int,
    ) -> list[ProviderContribution]:
        """Measure source overlap only inside the current bounded response.

        This is intentionally not presented as database recall or coverage: a
        provider may have more records beyond the requested limit/cursor.
        """

        canonical_by_provider = ScholarService._canonical_ids_by_provider(
            raw_ids_by_provider, record_id_map
        )
        occurrence_count = {
            canonical_id: sum(
                canonical_id in provider_ids for provider_ids in canonical_by_provider.values()
            )
            for canonical_id in set().union(*canonical_by_provider.values())
        }
        return [
            ProviderContribution(
                provider=provider,
                raw_record_count=len(raw_ids_by_provider[provider]),
                canonical_record_count=len(canonical_ids),
                unique_canonical_count=sum(
                    occurrence_count[canonical_id] == 1 for canonical_id in canonical_ids
                ),
                overlap_canonical_count=sum(
                    occurrence_count[canonical_id] > 1 for canonical_id in canonical_ids
                ),
                result_share=(len(canonical_ids) / canonical_total if canonical_total else 0.0),
            )
            for provider, canonical_ids in canonical_by_provider.items()
        ]

    @staticmethod
    def _provider_overlaps(
        raw_ids_by_provider: dict[str, list[str]],
        record_id_map: dict[str, str],
    ) -> list[ProviderOverlap]:
        canonical_by_provider = ScholarService._canonical_ids_by_provider(
            raw_ids_by_provider, record_id_map
        )
        overlaps: list[ProviderOverlap] = []
        for left_provider, right_provider in combinations(canonical_by_provider, 2):
            left = canonical_by_provider[left_provider]
            right = canonical_by_provider[right_provider]
            shared_count = len(left & right)
            union_count = len(left | right)
            smaller_count = min(len(left), len(right))
            overlaps.append(
                ProviderOverlap(
                    left_provider=left_provider,
                    right_provider=right_provider,
                    shared_canonical_count=shared_count,
                    union_canonical_count=union_count,
                    jaccard=shared_count / union_count if union_count else 0.0,
                    overlap_coefficient=(shared_count / smaller_count if smaller_count else 0.0),
                )
            )
        return overlaps

    @staticmethod
    def _canonical_ids_by_provider(
        raw_ids_by_provider: dict[str, list[str]],
        record_id_map: dict[str, str],
    ) -> dict[str, set[str]]:
        return {
            provider: {record_id_map.get(record_id, record_id) for record_id in raw_ids}
            for provider, raw_ids in raw_ids_by_provider.items()
        }

    @staticmethod
    def _unique_models(values: list):
        result = []
        seen: set[str] = set()
        for value in values:
            marker = value.model_dump_json()
            if marker not in seen:
                seen.add(marker)
                result.append(value)
        return result

    def _resolve_identities(self, papers: list[Paper]):
        events = self.store.list_identity_events(active_only=True) if self.store else []
        return resolve_identities(papers, events=events)

    @staticmethod
    def _append_once(values: list[str], value: str) -> None:
        if value not in values:
            values.append(value)

    @staticmethod
    def _validated_identifier(identifier: str) -> str:
        normalized = identifier.strip()
        if not normalized:
            raise ValueError("identifier must not be blank")
        return normalized

    @staticmethod
    def _validate_relation_limit(limit: int) -> None:
        # This is a library invariant, not merely CLI/API validation: MCP and
        # direct Python callers must receive the same bounded-work contract.
        if not 1 <= limit <= 1000:
            raise ValueError("relation limit must be between 1 and 1000")

    @staticmethod
    def _overall_status(reports: list[ProviderReport], has_results: bool) -> RunStatus:
        active = [report for report in reports if report.status != RunStatus.SKIPPED]
        if not active:
            return RunStatus.DEGRADED
        # A provider-level partial batch contains usable results plus an
        # explicitly disclosed gap. It must never collapse to FAILED merely
        # because there is no separate COMPLETE provider in the same request.
        if any(report.status == RunStatus.PARTIAL for report in active):
            return RunStatus.PARTIAL
        successes = sum(report.status in {RunStatus.COMPLETE, RunStatus.EMPTY} for report in active)
        if successes == len(active):
            return RunStatus.COMPLETE if has_results else RunStatus.EMPTY
        if successes:
            return RunStatus.PARTIAL
        if any(report.status == RunStatus.THROTTLED for report in reports):
            return RunStatus.THROTTLED
        return RunStatus.FAILED

    async def close(self) -> None:
        await asyncio.gather(*(provider.close() for provider in self.providers.values()))
        if self.store:
            self.store.close()
