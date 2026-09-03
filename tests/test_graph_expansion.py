from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from scholarly_retrieval.models import (
    CitationCountClaim,
    ExpansionDirection,
    GraphExpansionQuery,
    IdentifierClaim,
    IdentifierScheme,
    Paper,
    Provenance,
    ProviderBatch,
    RunStatus,
    SearchQuery,
    SourceRecord,
)
from scholarly_retrieval.providers.base import ProviderCapabilities, ScholarlyProvider
from scholarly_retrieval.service import ScholarService


class ExpansionFixtureProvider(ScholarlyProvider):
    name = "graph"
    capabilities = ProviderCapabilities(
        keyword_search=True,
        resolve_id=True,
        references="list",
        citations="list",
    )

    reference_map = {
        "S": ["A", "B"],
        "E": ["B", "D"],
        "A": ["C"],
        "B": ["C", "D"],
    }
    citation_map = {"S": ["X"]}

    async def search(self, query: SearchQuery) -> ProviderBatch:
        return ProviderBatch()

    async def resolve(self, identifier: str) -> Paper | None:
        return self._paper(identifier)

    async def references(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        identifiers = self.reference_map.get(identifier, [])
        return ProviderBatch(
            papers=[self._paper(item) for item in identifiers[:limit]],
            total_available=len(identifiers),
            truncated=len(identifiers) > limit,
        )

    async def citations(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        identifiers = self.citation_map.get(identifier, [])
        return ProviderBatch(
            papers=[self._paper(item) for item in identifiers[:limit]],
            total_available=len(identifiers),
            truncated=len(identifiers) > limit,
        )

    @staticmethod
    def _paper(identifier: str) -> Paper:
        years = {
            "S": 2000,
            "E": 2021,
            "A": 2010,
            "B": 2020,
            "C": 2022,
            "X": 2024,
        }
        work_types = {
            "S": "editorial",
            "E": "article",
            "A": "article",
            "B": "conference-paper",
            "C": "article",
            "X": "article",
        }
        return Paper(
            record_id=f"graph:{identifier}",
            title=f"Paper {identifier}",
            publication_year=years.get(identifier),
            work_type=work_types.get(identifier),
        )


class EvidenceProvider(ScholarlyProvider):
    capabilities = ProviderCapabilities(
        keyword_search=True,
        resolve_id=True,
        references="list",
    )

    def __init__(
        self,
        name: str,
        citation_count: int,
        target_doi: str = "10.1000/target",
    ) -> None:
        self.name = name
        self.citation_count = citation_count
        self.target_doi = target_doi

    async def search(self, query: SearchQuery) -> ProviderBatch:
        return ProviderBatch()

    async def resolve(self, identifier: str) -> Paper | None:
        return self._paper("seed", "10.1000/seed", 0)

    async def references(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        return ProviderBatch(
            papers=[self._paper("target", self.target_doi, self.citation_count)]
        )

    async def citations(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        return ProviderBatch()

    def _paper(self, local_id: str, doi: str, count: int) -> Paper:
        provenance = Provenance(provider=self.name, source_record_id=local_id)
        return Paper(
            record_id=f"{self.name}:{local_id}",
            title=f"Paper {local_id}",
            identifiers=[
                IdentifierClaim(
                    scheme=IdentifierScheme.DOI,
                    value=doi,
                    provenance=provenance,
                )
            ],
            citation_counts=[
                CitationCountClaim(
                    provider=self.name,
                    count=count,
                    source_record_id=local_id,
                )
            ],
            source_records=[SourceRecord(provider=self.name, source_record_id=local_id)],
        )


class BranchFailureProvider(ExpansionFixtureProvider):
    async def references(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        if identifier == "A":
            raise RuntimeError("injected branch failure")
        return await super().references(identifier, limit=limit)


class MissingSeedProvider(ExpansionFixtureProvider):
    async def resolve(self, identifier: str) -> Paper | None:
        if identifier == "MISSING":
            return None
        return await super().resolve(identifier)


class CheckpointCrash(BaseException):
    pass


class CheckpointProvider(ExpansionFixtureProvider):
    def __init__(self) -> None:
        self.fail_on_a = True
        self.resolve_calls: list[str] = []

    async def resolve(self, identifier: str) -> Paper | None:
        self.resolve_calls.append(identifier)
        return await super().resolve(identifier)

    async def references(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        if identifier == "A" and self.fail_on_a:
            raise CheckpointCrash("simulated process loss")
        return await super().references(identifier, limit=limit)


def test_two_hop_expansion_is_deterministic_and_retains_multiple_paths() -> None:
    async def scenario() -> None:
        service = ScholarService([ExpansionFixtureProvider()])
        result = await service.expand(
            GraphExpansionQuery(
                identifier="S",
                direction=ExpansionDirection.REFERENCES,
                depth=2,
                frontier_cap=10,
                top_k=10,
                per_node_limit=10,
            )
        )

        assert result.status == RunStatus.COMPLETE
        assert [paper.record_id for paper in result.papers] == [
            "graph:S",
            "graph:A",
            "graph:B",
            "graph:C",
            "graph:D",
        ]
        assert {(edge.citing_record_id, edge.cited_record_id) for edge in result.edges} == {
            ("graph:S", "graph:A"),
            ("graph:S", "graph:B"),
            ("graph:A", "graph:C"),
            ("graph:B", "graph:C"),
            ("graph:B", "graph:D"),
        }
        paths_to_c = [path for path in result.discovery_paths if path.target_record_id == "graph:C"]
        assert {path.predecessor_record_id for path in paths_to_c} == {"graph:A", "graph:B"}
        assert result.raw_record_count == 5
        assert result.manifestation_count == 5
        assert result.work_family_count == 5
        assert result.expanded_node_count == 3
        assert result.depth_reached == 2
        assert result.truncated is False
        assert result.fingerprint is not None
        assert [path.discovery_order for path in result.discovery_paths] == list(
            range(len(result.discovery_paths))
        )
        assert all(
            path.score is None and path.score_method is None for path in result.discovery_paths
        )
        assert {
            (
                similarity.left_record_id,
                similarity.right_record_id,
                tuple(similarity.shared_record_ids),
            )
            for similarity in result.co_citations
        } == {
            ("graph:A", "graph:B", ("graph:S",)),
            ("graph:C", "graph:D", ("graph:B",)),
        }
        assert [
            (
                similarity.left_record_id,
                similarity.right_record_id,
                similarity.shared_count,
                similarity.shared_record_ids,
            )
            for similarity in result.bibliographic_couplings
        ] == [("graph:A", "graph:B", 1, ["graph:C"])]
        assert result.rankings[0].record_id == "graph:C"
        assert sum(item.score for item in result.rankings) == pytest.approx(1.0)
        assert result.rankings[0].components["incoming_edge_count"] == 2.0

    asyncio.run(scenario())


def test_whole_graph_resume_starts_at_last_complete_layer() -> None:
    database = Path.cwd() / f".test-graph-checkpoint-{uuid4()}.sqlite3"

    async def scenario() -> None:
        provider = CheckpointProvider()
        service = ScholarService([provider], database_path=database)
        query = GraphExpansionQuery(
            identifier="S",
            direction=ExpansionDirection.REFERENCES,
            depth=2,
            top_k=10,
            per_node_limit=10,
        )

        with pytest.raises(AttributeError):
            await service.expand(query)
        assert service.store is not None
        assert service.store.stats()["cursor_checkpoints"] == 1
        seed_resolves_before_resume = provider.resolve_calls.count("S")

        provider.fail_on_a = False
        resumed = await service.expand(query)
        assert {paper.record_id for paper in resumed.papers} >= {
            "graph:S",
            "graph:A",
            "graph:B",
            "graph:C",
        }
        # Resume traverses A/B directly and does not repeat the top-level seed lookup.
        assert provider.resolve_calls.count("S") == seed_resolves_before_resume
        assert service.store.stats()["cursor_checkpoints"] == 0
        await service.close()

    try:
        asyncio.run(scenario())
    finally:
        for suffix in ("", "-wal", "-shm"):
            Path(f"{database}{suffix}").unlink(missing_ok=True)


def test_provider_limit_is_attached_to_the_discovery_path() -> None:
    async def scenario() -> None:
        service = ScholarService([ExpansionFixtureProvider()])
        result = await service.expand(
            GraphExpansionQuery(
                identifier="S",
                direction="references",
                depth=1,
                top_k=10,
                per_node_limit=1,
            )
        )

        assert result.truncation_reasons == ["provider_limit"]
        assert result.discovery_paths[1].target_record_id == "graph:A"
        assert result.discovery_paths[1].branch_truncated is True
        assert result.discovery_paths[1].continuations == {}

    asyncio.run(scenario())


def test_frontier_and_top_k_budgets_are_visible_and_leave_no_dangling_edges() -> None:
    async def scenario() -> None:
        service = ScholarService([ExpansionFixtureProvider()])
        frontier_limited = await service.expand(
            GraphExpansionQuery(
                identifier="S",
                direction="references",
                depth=2,
                frontier_cap=1,
                top_k=10,
            )
        )
        top_k_limited = await service.expand(
            GraphExpansionQuery(
                identifier="S",
                direction="references",
                depth=2,
                frontier_cap=10,
                top_k=1,
            )
        )

        assert frontier_limited.truncation_reasons == ["frontier_cap"]
        assert {paper.record_id for paper in frontier_limited.papers} == {
            "graph:S",
            "graph:A",
            "graph:B",
            "graph:C",
        }
        assert top_k_limited.truncation_reasons == ["top_k"]
        assert [paper.record_id for paper in top_k_limited.papers] == ["graph:S", "graph:A"]
        allowed = {paper.record_id for paper in top_k_limited.papers}
        assert all(
            edge.citing_record_id in allowed and edge.cited_record_id in allowed
            for edge in top_k_limited.edges
        )

    asyncio.run(scenario())


def test_bidirectional_expansion_keeps_citation_orientation_explicit() -> None:
    async def scenario() -> None:
        service = ScholarService([ExpansionFixtureProvider()])
        result = await service.expand(
            GraphExpansionQuery(identifier="S", direction="both", depth=1, top_k=10)
        )

        edges = {(edge.citing_record_id, edge.cited_record_id) for edge in result.edges}
        assert ("graph:S", "graph:A") in edges
        assert ("graph:S", "graph:B") in edges
        assert ("graph:X", "graph:S") in edges

    asyncio.run(scenario())


def test_visible_edge_unions_provider_assertions_without_summing_counts() -> None:
    async def scenario() -> None:
        service = ScholarService(
            [EvidenceProvider("source_a", 7), EvidenceProvider("source_b", 11)]
        )
        result = await service.references("10.1000/seed", limit=10)

        assert len(result.papers) == 1
        assert len(result.edges) == 1
        assert len(result.edges[0].assertions) == 2
        assert {item.provenance.provider for item in result.edges[0].assertions} == {
            "source_a",
            "source_b",
        }
        assert [claim.count for claim in result.papers[0].citation_counts] == [7, 11]
        contributions = {item.provider: item for item in result.provider_contributions}
        assert contributions["source_a"].raw_record_count == 1
        assert contributions["source_a"].canonical_record_count == 1
        assert contributions["source_a"].unique_canonical_count == 0
        assert contributions["source_a"].overlap_canonical_count == 1
        assert contributions["source_a"].result_share == 1.0
        assert contributions["source_b"] == contributions["source_a"].model_copy(
            update={"provider": "source_b"}
        )
        assert len(result.provider_overlaps) == 1
        assert result.provider_overlaps[0].shared_canonical_count == 1
        assert result.provider_overlaps[0].union_canonical_count == 1
        assert result.provider_overlaps[0].jaccard == 1.0
        assert result.provider_overlaps[0].overlap_coefficient == 1.0
        assert result.raw_record_count == 2
        assert result.manifestation_count == 2  # seed plus the merged target
        assert result.work_family_count == 2

    asyncio.run(scenario())


def test_relation_contributions_report_unique_gain_within_bounded_result() -> None:
    async def scenario() -> None:
        service = ScholarService(
            [
                EvidenceProvider("source_a", 7, "10.1000/target-a"),
                EvidenceProvider("source_b", 11, "10.1000/target-b"),
            ]
        )
        result = await service.references("10.1000/seed", limit=10)

        contributions = {item.provider: item for item in result.provider_contributions}
        assert len(result.papers) == 2
        assert contributions["source_a"].unique_canonical_count == 1
        assert contributions["source_a"].overlap_canonical_count == 0
        assert contributions["source_a"].result_share == 0.5
        assert contributions["source_b"].unique_canonical_count == 1
        assert result.provider_overlaps[0].shared_canonical_count == 0
        assert result.provider_overlaps[0].union_canonical_count == 2
        assert result.provider_overlaps[0].jaccard == 0.0
        assert result.provider_overlaps[0].overlap_coefficient == 0.0

    asyncio.run(scenario())


def test_one_failed_second_hop_branch_is_partial_and_contextualized() -> None:
    async def scenario() -> None:
        service = ScholarService([BranchFailureProvider()])
        query = GraphExpansionQuery(
            identifier="S",
            direction="references",
            depth=2,
            frontier_cap=10,
            top_k=10,
        )
        first = await service.expand(query)
        second = await service.expand(query)

        assert first.status == RunStatus.PARTIAL
        failure = next(
            report for report in first.provider_reports if report.provider == "aggregate"
        )
        assert failure.error_code == "RuntimeError"
        assert failure.context == {"node": "graph:A", "depth": 2}
        assert {paper.record_id for paper in first.papers} >= {"graph:C", "graph:D"}
        assert first.fingerprint == second.fingerprint

    asyncio.run(scenario())


def test_multi_seed_expansion_deduplicates_frontier_and_retains_each_origin() -> None:
    async def scenario() -> None:
        service = ScholarService([ExpansionFixtureProvider()])
        result = await service.expand(
            GraphExpansionQuery(
                identifiers=["S", "E"],
                direction="references",
                depth=1,
                top_k=10,
            )
        )

        assert {paper.record_id for paper in result.seeds} == {"graph:S", "graph:E"}
        assert {paper.record_id for paper in result.papers} == {
            "graph:S",
            "graph:E",
            "graph:A",
            "graph:B",
            "graph:D",
        }
        assert result.expanded_node_count == 2
        assert result.missing_seed_identifiers == []
        paths_to_b = [path for path in result.discovery_paths if path.target_record_id == "graph:B"]
        assert {path.seed_record_id for path in paths_to_b} == {"graph:S", "graph:E"}
        # The common reference B provides explainable coupling between both seeds.
        coupling = next(
            item
            for item in result.bibliographic_couplings
            if {item.left_record_id, item.right_record_id} == {"graph:S", "graph:E"}
        )
        assert coupling.shared_record_ids == ["graph:B"]

    asyncio.run(scenario())


def test_missing_seed_is_partial_but_all_missing_seeds_fail() -> None:
    async def scenario() -> None:
        service = ScholarService([MissingSeedProvider()])
        partial = await service.expand(
            GraphExpansionQuery(
                identifiers=["S", "MISSING"],
                direction="references",
                depth=1,
            )
        )

        assert partial.status == RunStatus.PARTIAL
        assert partial.missing_seed_identifiers == ["MISSING"]
        assert [paper.record_id for paper in partial.seeds] == ["graph:S"]
        failure = next(
            report for report in partial.provider_reports if report.operation == "expand_seed"
        )
        assert failure.error_code == "seed_not_resolved"
        assert failure.context == {"seed_identifier": "MISSING"}

        with pytest.raises(
            LookupError,
            match=r"no seed papers resolved for MISSING; provider outcomes: .*graph.*empty",
        ):
            await service.expand(GraphExpansionQuery(identifiers=["MISSING"]))

    asyncio.run(scenario())


def test_candidate_filters_are_auditable_and_do_not_consume_top_k() -> None:
    async def scenario() -> None:
        service = ScholarService([ExpansionFixtureProvider()])
        result = await service.expand(
            GraphExpansionQuery(
                identifier="S",
                direction="references",
                depth=1,
                top_k=1,
                year_from=2015,
                year_to=2021,
                include_work_types=[" Conference-Paper "],
            )
        )

        # The seed is intentionally outside the range: candidate filters must
        # never erase papers the user explicitly selected as graph roots.
        assert {paper.record_id for paper in result.papers} == {"graph:S", "graph:B"}
        assert result.truncation_reasons == []
        assert result.filtered_candidate_count == 1
        exclusion = result.filter_exclusions[0]
        assert exclusion.record_id == "graph:A"
        assert set(exclusion.reasons) == {
            "year_before_range",
            "work_type_not_included",
        }
        assert all("graph:A" not in edge.model_dump_json() for edge in result.edges)

    asyncio.run(scenario())


def test_missing_filter_metadata_has_distinct_reasons() -> None:
    async def scenario() -> None:
        service = ScholarService([ExpansionFixtureProvider()])
        result = await service.expand(
            GraphExpansionQuery(
                identifier="E",
                direction="references",
                depth=1,
                year_from=2015,
                include_work_types=["conference-paper"],
            )
        )

        exclusion = next(item for item in result.filter_exclusions if item.record_id == "graph:D")
        assert set(exclusion.reasons) == {
            "publication_year_missing",
            "work_type_missing",
        }
        assert result.filtered_candidate_count == 1

    asyncio.run(scenario())


def test_repeated_filtered_candidate_counts_once_and_never_creates_edges() -> None:
    async def scenario() -> None:
        service = ScholarService([ExpansionFixtureProvider()])
        result = await service.expand(
            GraphExpansionQuery(
                identifier="S",
                direction="references",
                depth=2,
                year_to=2021,
            )
        )

        assert result.filtered_candidate_count == 2
        assert sum(item.record_id == "graph:C" for item in result.filter_exclusions) == 2
        assert {paper.record_id for paper in result.papers} == {
            "graph:S",
            "graph:A",
            "graph:B",
        }
        allowed = {paper.record_id for paper in result.papers}
        assert all(
            edge.citing_record_id in allowed and edge.cited_record_id in allowed
            for edge in result.edges
        )

    asyncio.run(scenario())


def test_runtime_budget_stops_at_a_layer_boundary() -> None:
    async def scenario() -> None:
        timestamps = iter([0.0, 0.0, 2.0])
        service = ScholarService([ExpansionFixtureProvider()], clock=lambda: next(timestamps))
        result = await service.expand(
            GraphExpansionQuery(
                identifier="S",
                direction="references",
                depth=2,
                max_runtime_seconds=1.0,
            )
        )

        assert result.truncation_reasons == ["runtime_budget"]
        assert result.depth_reached == 1
        assert result.expanded_node_count == 1
        assert {paper.record_id for paper in result.papers} == {
            "graph:S",
            "graph:A",
            "graph:B",
        }

    asyncio.run(scenario())
