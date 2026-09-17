"""Provider-neutral domain and result contracts."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class IdentifierScheme(StrEnum):
    DOI = "doi"
    ARXIV = "arxiv"
    PMID = "pmid"
    PMCID = "pmcid"
    OPENALEX = "openalex"
    SEMANTIC_SCHOLAR = "semantic_scholar"
    CROSSREF = "crossref"
    GOOGLE_SCHOLAR = "google_scholar"
    DBLP = "dblp"
    OMID = "omid"
    OPENAIRE = "openaire"
    INSPIRE = "inspire"
    OPENREVIEW = "openreview"
    ACL_ANTHOLOGY = "acl_anthology"


class RunStatus(StrEnum):
    """Execution state; EMPTY, THROTTLED, and SKIPPED are intentionally distinct."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    DEGRADED = "degraded"
    FAILED = "failed"
    EMPTY = "empty"
    THROTTLED = "throttled"
    SKIPPED = "skipped"


class RelationKind(StrEnum):
    REFERENCES = "references"
    CITES = "cites"


class CitationEvidenceType(StrEnum):
    PROVIDER_GRAPH = "provider_graph"
    DEPOSITOR_METADATA = "depositor_metadata"
    FULLTEXT_ANCHOR = "fulltext_anchor"


class CitationVerificationStatus(StrEnum):
    PROVIDER_ASSERTED = "provider_asserted"
    VERIFIED = "verified"


class ReferenceEvidenceLevel(StrEnum):
    VERIFIED_ANCHOR = "verified_anchor"
    BIBLIOGRAPHY_ONLY = "bibliography_only"


class ReferenceLinkStatus(StrEnum):
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    UNRESOLVED = "unresolved"


class ExpansionDirection(StrEnum):
    REFERENCES = "references"
    CITATIONS = "citations"
    BOTH = "both"


class StopRule(StrEnum):
    BUDGET = "budget"


class RetrievalMethod(StrEnum):
    OPENALEX_SEMANTIC = "openalex_semantic"
    SEMANTIC_SCHOLAR_RECOMMENDATION = "semantic_scholar_recommendation"


class SearchSort(StrEnum):
    RELEVANCE = "relevance"
    NEWEST = "newest"
    OLDEST = "oldest"
    CITATIONS = "citations"


class SearchField(StrEnum):
    ALL = "all"
    TITLE = "title"
    ABSTRACT = "abstract"
    AUTHOR = "author"
    VENUE = "venue"
    FIELD = "field"


class SearchExpressionOperator(StrEnum):
    TERM = "term"
    PHRASE = "phrase"
    AND = "and"
    OR = "or"
    NOT = "not"


class GraphSimilarityKind(StrEnum):
    CO_CITATION = "co_citation"
    BIBLIOGRAPHIC_COUPLING = "bibliographic_coupling"


class GraphFilterReason(StrEnum):
    PUBLICATION_YEAR_MISSING = "publication_year_missing"
    YEAR_BEFORE_RANGE = "year_before_range"
    YEAR_AFTER_RANGE = "year_after_range"
    WORK_TYPE_MISSING = "work_type_missing"
    WORK_TYPE_NOT_INCLUDED = "work_type_not_included"


class IdentityDecisionKind(StrEnum):
    MUST_LINK = "must_link"
    CANNOT_LINK = "cannot_link"
    REVIEW = "review"


class EntityGroupingStatus(StrEnum):
    """Whether an entity grouping is evidence-backed or an explicit singleton."""

    ASSERTED = "asserted"
    SINGLETON = "singleton"


class EntityRelationKind(StrEnum):
    SAME_MANIFESTATION = "same_manifestation"
    VERSION_OF = "version_of"
    DIFFERENT_WORK = "different_work"


class IdentityEventAction(StrEnum):
    MERGE = "merge"
    SPLIT = "split"
    DEFER = "defer"
    REVERT = "revert"


class ArtifactKind(StrEnum):
    LANDING_PAGE = "landing_page"
    PDF = "pdf"


class Provenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    source_record_id: str
    retrieved_at: datetime = Field(default_factory=utc_now)
    source_url: str | None = None


class IdentifierClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scheme: IdentifierScheme
    value: str
    authority: str | None = None
    confidence: float = Field(default=1.0, ge=0, le=1)
    provenance: Provenance

    @model_validator(mode="after")
    def fill_issuing_authority(self) -> IdentifierClaim:
        if self.authority is None:
            self.authority = {
                IdentifierScheme.DOI: "DOI Registration Agency",
                IdentifierScheme.ARXIV: "arXiv",
                IdentifierScheme.PMID: "U.S. National Library of Medicine",
                IdentifierScheme.PMCID: "PubMed Central",
                IdentifierScheme.OPENALEX: "OpenAlex",
                IdentifierScheme.SEMANTIC_SCHOLAR: "Semantic Scholar",
                IdentifierScheme.CROSSREF: "Crossref",
                IdentifierScheme.GOOGLE_SCHOLAR: "Google Scholar",
                IdentifierScheme.DBLP: "DBLP",
                IdentifierScheme.OMID: "OpenCitations Meta",
                IdentifierScheme.OPENAIRE: "OpenAIRE",
                IdentifierScheme.INSPIRE: "INSPIRE",
                IdentifierScheme.OPENREVIEW: "OpenReview",
                IdentifierScheme.ACL_ANTHOLOGY: "ACL Anthology",
            }[self.scheme]
        return self


class Author(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    orcid: str | None = None
    provider_ids: dict[str, str] = Field(default_factory=dict)


class SourceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    source_record_id: str
    source_url: str | None = None
    provider_rank: int | None = None
    provider_score: float | None = None
    retrieval_method: RetrievalMethod | None = None
    retrieval_context: dict[str, Any] = Field(default_factory=dict)
    retrieved_at: datetime = Field(default_factory=utc_now)


class CitationCountClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    count: int = Field(ge=0)
    source_record_id: str
    retrieved_at: datetime = Field(default_factory=utc_now)


class FieldClaim(BaseModel):
    """A provider's original value for one canonical paper field."""

    model_config = ConfigDict(extra="forbid")

    value: Any
    provenance: Provenance
    confidence: float | None = Field(default=None, ge=0, le=1)


class Paper(BaseModel):
    """A current canonical view with all source claims retained."""

    model_config = ConfigDict(extra="forbid")

    record_id: str
    work_family_id: str | None = None
    title: str
    abstract: str | None = None
    authors: list[Author] = Field(default_factory=list)
    publication_date: date | None = None
    publication_year: int | None = None
    work_type: str | None = None
    venue: str | None = None
    fields_of_study: list[str] = Field(default_factory=list)
    language: str | None = None
    open_access: bool | None = None
    landing_page_url: str | None = None
    pdf_url: str | None = None
    identifiers: list[IdentifierClaim] = Field(default_factory=list)
    citation_counts: list[CitationCountClaim] = Field(default_factory=list)
    source_records: list[SourceRecord] = Field(default_factory=list)
    field_provenance: dict[str, list[Provenance]] = Field(default_factory=dict)
    field_claims: dict[str, list[FieldClaim]] = Field(default_factory=dict)

    @field_validator("title")
    @classmethod
    def title_must_not_be_blank(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("paper title must not be blank")
        return value

    @model_validator(mode="after")
    def preserve_values_for_provenance(self) -> Paper:
        """Back field provenance with the actual value seen at that source."""

        for field, provenances in self.field_provenance.items():
            if field in self.field_claims or not hasattr(self, field):
                continue
            value = getattr(self, field)
            if value is None:
                continue
            if isinstance(value, list):
                claim_value = [
                    item.model_dump(mode="json") if isinstance(item, BaseModel) else item
                    for item in value
                ]
            elif isinstance(value, (date, datetime)):
                claim_value = value.isoformat()
            else:
                claim_value = value
            self.field_claims[field] = [
                FieldClaim(value=claim_value, provenance=provenance) for provenance in provenances
            ]
        return self


class SearchExpression(BaseModel):
    """Provider-neutral Boolean/phrase query tree.

    ``text`` in SearchQuery remains the bounded upstream recall query. This AST
    states the exact portable semantics and is evaluated locally whenever a
    provider cannot compile it without loss.
    """

    model_config = ConfigDict(extra="forbid")

    operator: SearchExpressionOperator
    field: SearchField = SearchField.ALL
    value: str | None = None
    children: list[SearchExpression] = Field(default_factory=list)

    @model_validator(mode="after")
    def shape_must_match_operator(self) -> SearchExpression:
        if self.operator in {
            SearchExpressionOperator.TERM,
            SearchExpressionOperator.PHRASE,
        }:
            if self.value is None or not " ".join(self.value.split()):
                raise ValueError("term and phrase expressions require a value")
            if self.children:
                raise ValueError("term and phrase expressions cannot have children")
            self.value = " ".join(self.value.split())
            return self
        if self.value is not None:
            raise ValueError("Boolean expressions cannot have a value")
        if self.field != SearchField.ALL:
            raise ValueError("Boolean expressions cannot select a field")
        if self.operator == SearchExpressionOperator.NOT and len(self.children) != 1:
            raise ValueError("not expressions require exactly one child")
        if (
            self.operator
            in {
                SearchExpressionOperator.AND,
                SearchExpressionOperator.OR,
            }
            and len(self.children) < 2
        ):
            raise ValueError("and/or expressions require at least two children")
        return self


class SearchQuery(BaseModel):
    """Provider-neutral subset currently supported by every search adapter.

    Provider reports must disclose whether each requested filter ran upstream,
    locally, or was unsupported; connectors may not silently drop a filter.
    """

    model_config = ConfigDict(extra="forbid")

    text: str
    expression: SearchExpression | None = None
    year_from: int | None = Field(default=None, ge=1000, le=3000)
    year_to: int | None = Field(default=None, ge=1000, le=3000)
    author: str | None = None
    open_access: bool | None = None
    title: str | None = None
    abstract: str | None = None
    venue: str | None = None
    field: str | None = None
    work_types: list[str] = Field(default_factory=list, max_length=20)
    min_citations: int | None = Field(default=None, ge=0)
    sort: SearchSort = SearchSort.RELEVANCE
    limit: int = Field(default=20, ge=1, le=100)

    @field_validator("text")
    @classmethod
    def query_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("query text must not be blank")
        return value

    @field_validator("author")
    @classmethod
    def author_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("author filter must not be blank")
        return normalized

    @field_validator("title", "abstract", "venue", "field")
    @classmethod
    def advanced_text_filters_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("advanced text filters must not be blank")
        return normalized

    @field_validator("work_types")
    @classmethod
    def work_types_must_not_be_blank(cls, values: list[str]) -> list[str]:
        normalized = [" ".join(value.split()).casefold() for value in values]
        if any(not value for value in normalized):
            raise ValueError("work types must not contain blanks")
        return list(dict.fromkeys(normalized))

    @model_validator(mode="after")
    def year_range_must_be_ordered(self) -> SearchQuery:
        if (
            self.year_from is not None
            and self.year_to is not None
            and self.year_from > self.year_to
        ):
            raise ValueError("year_from must not be greater than year_to")
        return self


class RelatedQuery(BaseModel):
    """Portable related-paper request spanning text and seed recommenders."""

    model_config = ConfigDict(extra="forbid")

    text: str | None = Field(default=None, max_length=2000)
    positive_identifiers: list[str] = Field(default_factory=list, max_length=10)
    negative_identifiers: list[str] = Field(default_factory=list, max_length=10)
    year_from: int | None = Field(default=None, ge=1000, le=3000)
    year_to: int | None = Field(default=None, ge=1000, le=3000)
    open_access: bool | None = None
    include_work_types: list[str] = Field(default_factory=list, max_length=20)
    limit: int = Field(default=20, ge=1, le=100)
    rrf_k: int = Field(default=60, ge=1, le=1000)

    @field_validator("text")
    @classmethod
    def text_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("related text must not be blank")
        return normalized

    @field_validator("positive_identifiers", "negative_identifiers")
    @classmethod
    def related_identifiers_must_be_valid(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("related identifiers must not contain blanks")
        return list(dict.fromkeys(normalized))

    @field_validator("include_work_types")
    @classmethod
    def related_work_types_must_be_valid(cls, values: list[str]) -> list[str]:
        normalized = [" ".join(value.split()).casefold() for value in values]
        if any(not value for value in normalized):
            raise ValueError("included work types must not contain blanks")
        return list(dict.fromkeys(normalized))

    @model_validator(mode="after")
    def request_must_have_input_and_valid_years(self) -> RelatedQuery:
        if self.text is None and not self.positive_identifiers:
            raise ValueError("related requires text or a positive identifier")
        if set(self.positive_identifiers) & set(self.negative_identifiers):
            raise ValueError("an identifier cannot be both positive and negative")
        if (
            self.year_from is not None
            and self.year_to is not None
            and self.year_from > self.year_to
        ):
            raise ValueError("year_from must not be greater than year_to")
        return self


class ProviderReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    operation: str
    status: RunStatus
    retrieved_count: int = Field(default=0, ge=0)
    unresolved_count: int = Field(default=0, ge=0)
    total_available: int | None = Field(default=None, ge=0)
    truncated: bool = False
    next_cursor: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    filter_execution: dict[str, str] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)


class ProviderContribution(BaseModel):
    """One source's contribution within this bounded, deduplicated result set."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    raw_record_count: int = Field(ge=0)
    canonical_record_count: int = Field(ge=0)
    unique_canonical_count: int = Field(ge=0)
    overlap_canonical_count: int = Field(ge=0)
    result_share: float = Field(ge=0, le=1)


class ProviderOverlap(BaseModel):
    """Pairwise overlap inside one bounded provider-union response."""

    model_config = ConfigDict(extra="forbid")

    left_provider: str
    right_provider: str
    shared_canonical_count: int = Field(ge=0)
    union_canonical_count: int = Field(ge=0)
    jaccard: float = Field(ge=0, le=1)
    overlap_coefficient: float = Field(ge=0, le=1)


class ProviderBatch(BaseModel):
    """One provider page or bounded multi-page result.

    ``next_cursor`` is an opaque provider continuation token. Callers must never
    parse or synthesize it; persistence may store it for an exact resume.
    """

    model_config = ConfigDict(extra="forbid")

    papers: list[Paper] = Field(default_factory=list)
    total_available: int | None = Field(default=None, ge=0)
    truncated: bool = False
    next_cursor: str | None = None
    filter_execution: dict[str, str] = Field(default_factory=dict)
    unresolved_references: list[UnresolvedReference] = Field(default_factory=list)
    # Adapters may preserve useful rows when one page or source sub-cluster
    # fails. This override prevents such a bounded result being mislabeled as
    # complete by the service layer.
    status: RunStatus | None = None
    context: dict[str, Any] = Field(default_factory=dict)


class RetrievalEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    method: RetrievalMethod
    provider_rank: int = Field(ge=1)
    provider_score: float | None = None
    rrf_contribution: float = Field(gt=0)
    context: dict[str, Any] = Field(default_factory=dict)


class RelatedRanking(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_id: str
    rank: int = Field(ge=1)
    score: float = Field(gt=0)
    score_method: str = "rrf"
    evidence: list[RetrievalEvidence] = Field(min_length=1)


class UnresolvedReference(BaseModel):
    """A deposited reference that cannot yet be promoted to a canonical paper."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    seed_record_id: str
    ordinal: int = Field(ge=1)
    raw: dict[str, Any] = Field(default_factory=dict)
    title: str | None = None
    author: str | None = None
    publication_year: int | None = Field(default=None, ge=1000, le=3000)
    doi: str | None = None
    reason: str
    provenance: Provenance


class CitationContext(BaseModel):
    """One body passage containing a citation anchor for a bibliography item."""

    model_config = ConfigDict(extra="forbid")

    context_id: str
    text: str = Field(min_length=1)
    anchor_text: str | None = None
    section_title: str | None = None
    source_locator: str | None = None


class ExtractedReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reference_id: str
    ordinal: int = Field(ge=1)
    cited_in_text: bool
    evidence_level: ReferenceEvidenceLevel
    raw_text: str
    title: str | None = None
    authors: list[str] = Field(default_factory=list)
    publication_year: int | None = Field(default=None, ge=1000, le=3000)
    doi: str | None = None
    venue: str | None = None
    volume: str | None = None
    issue: str | None = None
    pages: str | None = None
    callout_count: int = Field(default=0, ge=0)
    contexts: list[CitationContext] = Field(default_factory=list)


class ReferenceExtractionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_format: str
    references: list[ExtractedReference]
    bibliography_entry_count: int = Field(ge=0)
    cited_entry_count: int = Field(ge=0)
    uncited_reference_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    processing_steps: list[str] = Field(default_factory=list)


class CitationAssertion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assertion_id: str | None = None
    subject_record_id: str
    object_record_id: str
    relation: RelationKind
    provenance: Provenance
    evidence_type: CitationEvidenceType = CitationEvidenceType.PROVIDER_GRAPH
    verification_status: CitationVerificationStatus = CitationVerificationStatus.PROVIDER_ASSERTED
    evidence: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def assign_stable_assertion_id(self) -> CitationAssertion:
        material = "\0".join(
            (
                self.subject_record_id,
                self.object_record_id,
                self.relation.value,
                self.provenance.provider,
                self.provenance.source_record_id,
                self.evidence_type.value,
            )
        )
        self.assertion_id = f"ca:{hashlib.sha256(material.encode()).hexdigest()[:24]}"
        return self


class VisibleCitationEdge(BaseModel):
    """One canonical citing -> cited edge backed by every raw assertion."""

    model_config = ConfigDict(extra="forbid")

    edge_id: str | None = None
    citing_record_id: str
    cited_record_id: str
    assertions: list[CitationAssertion]
    verification_status: CitationVerificationStatus = CitationVerificationStatus.PROVIDER_ASSERTED

    @model_validator(mode="after")
    def derive_identity_and_verification(self) -> VisibleCitationEdge:
        material = f"{self.citing_record_id}\0{self.cited_record_id}"
        self.edge_id = f"ce:{hashlib.sha256(material.encode()).hexdigest()[:24]}"
        self.verification_status = (
            CitationVerificationStatus.VERIFIED
            if any(
                assertion.verification_status == CitationVerificationStatus.VERIFIED
                for assertion in self.assertions
            )
            else CitationVerificationStatus.PROVIDER_ASSERTED
        )
        return self


class CitationEdgeStatusEvent(BaseModel):
    """Auditable monotonic verification upgrade for one visible citation edge."""

    model_config = ConfigDict(extra="forbid")

    event_id: str
    edge_id: str
    previous_status: CitationVerificationStatus
    new_status: CitationVerificationStatus
    triggering_assertion_id: str
    occurred_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def require_supported_upgrade(self) -> CitationEdgeStatusEvent:
        if not (
            self.previous_status == CitationVerificationStatus.PROVIDER_ASSERTED
            and self.new_status == CitationVerificationStatus.VERIFIED
        ):
            raise ValueError("citation edge status events must be provider_asserted -> verified")
        return self


class GraphSimilarity(BaseModel):
    """Explainable pair similarity derived only from visible citation edges."""

    model_config = ConfigDict(extra="forbid")

    kind: GraphSimilarityKind
    left_record_id: str
    right_record_id: str
    shared_count: int = Field(ge=1)
    shared_record_ids: list[str] = Field(min_length=1)


class GraphNodeRanking(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_id: str
    rank: int = Field(ge=1)
    score: float = Field(ge=0, le=1)
    score_method: str = "pagerank"
    components: dict[str, float] = Field(default_factory=dict)


class GraphFilterExclusion(BaseModel):
    """One candidate rejected before it can consume graph traversal budgets."""

    model_config = ConfigDict(extra="forbid")

    record_id: str
    predecessor_record_id: str
    depth: int = Field(ge=1)
    operation: str
    reasons: list[GraphFilterReason] = Field(min_length=1)


class DiscoveryPath(BaseModel):
    """One deterministic way a node entered a bounded local graph."""

    model_config = ConfigDict(extra="forbid")

    seed_record_id: str
    target_record_id: str
    predecessor_record_id: str | None = None
    depth: int = Field(ge=0)
    operation: str
    discovery_order: int = Field(ge=0)
    score: float | None = Field(default=None, ge=0)
    score_method: str | None = None
    branch_truncated: bool = False
    continuations: dict[str, str] = Field(default_factory=dict)


class GraphExpansionQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identifier: str | None = None
    identifiers: list[str] = Field(default_factory=list, max_length=20)
    direction: ExpansionDirection = ExpansionDirection.BOTH
    depth: int = Field(default=2, ge=1, le=3)
    frontier_cap: int = Field(default=200, ge=1, le=1000)
    top_k: int = Field(default=100, ge=1, le=5000)
    per_node_limit: int = Field(default=100, ge=1, le=1000)
    stop_rule: StopRule = StopRule.BUDGET
    year_from: int | None = Field(default=None, ge=1000, le=3000)
    year_to: int | None = Field(default=None, ge=1000, le=3000)
    include_work_types: list[str] = Field(default_factory=list, max_length=20)
    max_runtime_seconds: float | None = Field(default=None, ge=0.1, le=3600)

    @field_validator("identifier")
    @classmethod
    def identifier_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("identifier must not be blank")
        return normalized

    @field_validator("identifiers")
    @classmethod
    def identifiers_must_not_contain_blanks(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("seed identifiers must not contain blanks")
        return list(dict.fromkeys(normalized))

    @field_validator("include_work_types")
    @classmethod
    def work_types_must_not_contain_blanks(cls, values: list[str]) -> list[str]:
        normalized = [" ".join(value.split()).casefold() for value in values]
        if any(not value for value in normalized):
            raise ValueError("included work types must not contain blanks")
        return list(dict.fromkeys(normalized))

    @model_validator(mode="after")
    def at_least_one_seed_is_required(self) -> GraphExpansionQuery:
        if not self.seed_identifiers:
            raise ValueError("at least one seed identifier is required")
        if len(self.seed_identifiers) > 20:
            raise ValueError("at most 20 unique seed identifiers are allowed")
        if (
            self.year_from is not None
            and self.year_to is not None
            and self.year_from > self.year_to
        ):
            raise ValueError("year_from must not be greater than year_to")
        return self

    @property
    def seed_identifiers(self) -> list[str]:
        values = [*([self.identifier] if self.identifier else []), *self.identifiers]
        return list(dict.fromkeys(values))


class IdentityDecision(BaseModel):
    """Auditable pairwise evidence emitted by conservative entity resolution."""

    model_config = ConfigDict(extra="forbid")

    left_record_id: str
    right_record_id: str
    decision: IdentityDecisionKind
    reasons: list[str]
    shared_identifiers: list[str] = Field(default_factory=list)
    conflicting_identifiers: dict[str, list[str]] = Field(default_factory=dict)
    score: float | None = Field(default=None, ge=0, le=1)
    score_components: dict[str, float] = Field(default_factory=dict)


class IdentityResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    papers: list[Paper]
    decisions: list[IdentityDecision] = Field(default_factory=list)
    record_id_map: dict[str, str] = Field(default_factory=dict)


class Manifestation(BaseModel):
    """One provider-reconciled, externally observable scholarly object."""

    model_config = ConfigDict(extra="forbid")

    manifestation_id: str
    paper_record_id: str
    version_id: str
    work_family_id: str
    source_record_ids: list[str] = Field(default_factory=list)


class Version(BaseModel):
    """A version group; currently singleton unless explicit family evidence exists."""

    model_config = ConfigDict(extra="forbid")

    version_id: str
    work_family_id: str
    manifestation_ids: list[str] = Field(min_length=1)
    grouping_status: EntityGroupingStatus
    evidence: list[str] = Field(default_factory=list)


class WorkFamily(BaseModel):
    """A conceptual work containing one or more known versions/manifestations."""

    model_config = ConfigDict(extra="forbid")

    work_family_id: str
    title: str
    version_ids: list[str] = Field(min_length=1)
    manifestation_ids: list[str] = Field(min_length=1)
    grouping_status: EntityGroupingStatus
    evidence: list[str] = Field(default_factory=list)


class Artifact(BaseModel):
    """One retrievable landing-page/full-text artifact of a manifestation."""

    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    manifestation_id: str
    kind: ArtifactKind
    url: str
    open_access: bool | None = None


class EntityView(BaseModel):
    """Normalized Manifestation -> Version -> WorkFamily projection."""

    model_config = ConfigDict(extra="forbid")

    manifestations: list[Manifestation] = Field(default_factory=list)
    versions: list[Version] = Field(default_factory=list)
    work_families: list[WorkFamily] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)


class IdentityEvent(BaseModel):
    """Persisted human identity decision; rollback events preserve audit history."""

    model_config = ConfigDict(extra="forbid")

    event_id: str
    action: IdentityEventAction
    relation: EntityRelationKind
    left_record_id: str
    right_record_id: str
    reasons: list[str] = Field(min_length=1)
    created_at: datetime = Field(default_factory=utc_now)
    reverts_event_id: str | None = None
    reverted_by_event_id: str | None = None


class ReferenceCandidateScore(BaseModel):
    """Auditable field-level evidence for one reference-to-paper candidate."""

    model_config = ConfigDict(extra="forbid")

    record_id: str
    total_score: float = Field(ge=0, le=1)
    identifier_score: float | None = Field(default=None, ge=0, le=1)
    title_score: float | None = Field(default=None, ge=0, le=1)
    author_score: float | None = Field(default=None, ge=0, le=1)
    year_score: float | None = Field(default=None, ge=0, le=1)
    venue_score: float | None = Field(default=None, ge=0, le=1)
    compared_fields: list[str] = Field(default_factory=list)
    missing_candidate_fields: list[str] = Field(default_factory=list)


class ReferenceLink(BaseModel):
    """One extracted bibliography item linked conservatively to candidates."""

    model_config = ConfigDict(extra="forbid")

    reference: ExtractedReference
    status: ReferenceLinkStatus
    method: str
    candidates: list[Paper] = Field(default_factory=list)
    candidate_scores: list[ReferenceCandidateScore] = Field(default_factory=list)
    decision_reason: str = "not_evaluated"
    entity_view: EntityView = Field(default_factory=EntityView)
    provider_reports: list[ProviderReport] = Field(default_factory=list)


class ReferenceLinkingResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: str = "link_references"
    seed: Paper
    extraction: ReferenceExtractionResult
    status: RunStatus
    links: list[ReferenceLink]
    assertions: list[CitationAssertion] = Field(default_factory=list)
    edges: list[VisibleCitationEdge] = Field(default_factory=list)
    provider_reports: list[ProviderReport] = Field(default_factory=list)
    resolved_count: int = Field(ge=0)
    ambiguous_count: int = Field(ge=0)
    unresolved_count: int = Field(ge=0)
    fingerprint: str | None = None


class SearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: SearchQuery
    status: RunStatus
    papers: list[Paper]
    provider_reports: list[ProviderReport]
    identity_decisions: list[IdentityDecision] = Field(default_factory=list)
    raw_record_count: int = Field(ge=0)
    canonical_record_count: int = Field(ge=0)
    entity_view: EntityView = Field(default_factory=EntityView)
    truncated: bool = False
    fingerprint: str | None = None


class RelatedResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: RelatedQuery
    status: RunStatus
    papers: list[Paper]
    rankings: list[RelatedRanking]
    provider_reports: list[ProviderReport]
    identity_decisions: list[IdentityDecision] = Field(default_factory=list)
    raw_record_count: int = Field(ge=0)
    canonical_record_count: int = Field(ge=0)
    entity_view: EntityView = Field(default_factory=EntityView)
    truncated: bool = False
    fingerprint: str | None = None


class ResolveResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identifier: str
    status: RunStatus
    papers: list[Paper]
    provider_reports: list[ProviderReport]
    identity_decisions: list[IdentityDecision] = Field(default_factory=list)
    entity_view: EntityView = Field(default_factory=EntityView)
    fingerprint: str | None = None


class GraphResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: str
    seed: Paper
    status: RunStatus
    papers: list[Paper]
    assertions: list[CitationAssertion]
    edges: list[VisibleCitationEdge] = Field(default_factory=list)
    provider_reports: list[ProviderReport]
    provider_contributions: list[ProviderContribution] = Field(default_factory=list)
    provider_overlaps: list[ProviderOverlap] = Field(default_factory=list)
    unresolved_references: list[UnresolvedReference] = Field(default_factory=list)
    identity_decisions: list[IdentityDecision] = Field(default_factory=list)
    entity_view: EntityView = Field(default_factory=EntityView)
    raw_record_count: int = Field(default=0, ge=0)
    manifestation_count: int = Field(default=0, ge=0)
    work_family_count: int = Field(default=0, ge=0)
    truncated: bool = False
    fingerprint: str | None = None


class GraphExpansionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: str = "expand"
    query: GraphExpansionQuery
    seeds: list[Paper]
    missing_seed_identifiers: list[str] = Field(default_factory=list)
    status: RunStatus
    papers: list[Paper]
    edges: list[VisibleCitationEdge]
    co_citations: list[GraphSimilarity] = Field(default_factory=list)
    bibliographic_couplings: list[GraphSimilarity] = Field(default_factory=list)
    rankings: list[GraphNodeRanking] = Field(default_factory=list)
    filter_exclusions: list[GraphFilterExclusion] = Field(default_factory=list)
    filtered_candidate_count: int = Field(default=0, ge=0)
    assertions: list[CitationAssertion]
    discovery_paths: list[DiscoveryPath]
    provider_reports: list[ProviderReport]
    unresolved_references: list[UnresolvedReference] = Field(default_factory=list)
    identity_decisions: list[IdentityDecision] = Field(default_factory=list)
    raw_record_count: int = Field(ge=0)
    manifestation_count: int = Field(ge=0)
    work_family_count: int = Field(ge=0)
    entity_view: EntityView = Field(default_factory=EntityView)
    expanded_node_count: int = Field(ge=0)
    depth_reached: int = Field(ge=0)
    truncated: bool = False
    truncation_reasons: list[str] = Field(default_factory=list)
    fingerprint: str | None = None
