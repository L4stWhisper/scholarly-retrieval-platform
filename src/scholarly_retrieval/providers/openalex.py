"""OpenAlex adapter for works and their citation neighborhood."""

from __future__ import annotations

import os
from datetime import date
from typing import Any
from urllib.parse import quote

import httpx

from ..models import (
    Author,
    CitationCountClaim,
    IdentifierClaim,
    IdentifierScheme,
    Paper,
    Provenance,
    ProviderBatch,
    RelatedQuery,
    RetrievalMethod,
    SearchQuery,
    SearchSort,
    SourceRecord,
    UnresolvedReference,
    utc_now,
)
from ..normalization import normalize_doi, normalize_openalex_id
from ..reliability import ReliableHttpClient, RetryPolicy
from ..storage import SQLiteStore, make_checkpoint_key
from .base import ProviderCapabilities, ScholarlyProvider

WORK_FIELDS = ",".join(
    [
        "id",
        "doi",
        "title",
        "display_name",
        "abstract_inverted_index",
        "authorships",
        "publication_date",
        "publication_year",
        "type",
        "language",
        "primary_location",
        "open_access",
        "ids",
        "cited_by_count",
        "referenced_works",
        "relevance_score",
        "topics",
    ]
)

# OpenAlex ``works.type`` values accepted by the upstream ``type`` filter.
# Keep this allow-list explicit: neutral/user-defined work types must fall back
# to canonical local filtering rather than turning into an upstream HTTP 400.
OPENALEX_WORK_TYPES = frozenset(
    {
        "article",
        "book",
        "book-chapter",
        "book-review",
        "conference-abstract",
        "conference-paper",
        "data-paper",
        "dataset",
        "dissertation",
        "editorial",
        "erratum",
        "letter",
        "libguides",
        "other",
        "paratext",
        "peer-review",
        "preprint",
        "reference-entry",
        "report",
        "retraction",
        "review",
        "software",
        "software-paper",
        "standard",
        "supplementary-materials",
    }
)


class OpenAlexProvider(ScholarlyProvider):
    name = "openalex"
    capabilities = ProviderCapabilities(
        keyword_search=True,
        advanced_search=True,
        resolve_id=True,
        references="list",
        citations="list",
        related=True,
        fulltext=False,
        search_filter_execution={
            "text": "provider",
            "year_from": "provider",
            "year_to": "provider",
            "author": "provider",
            "open_access": "provider",
            "title": "provider",
            "min_citations": "provider",
            "sort": "provider",
        },
        pagination={"citations": "opaque_cursor", "references": "id_batch"},
        access_tier="demo_without_key_api_key_for_production",
        credential_variables=["OPENALEX_API_KEY", "OPENALEX_MAILTO"],
        terms_url="https://openalex.org/terms",
        redistribution_policy="open_metadata_subject_to_openalex_terms",
    )

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        api_key: str | None = None,
        mailto: str | None = None,
        store: SQLiteStore | None = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url="https://api.openalex.org",
            timeout=httpx.Timeout(20.0),
            headers={"User-Agent": "scholarly-retrieval-platform/0.1"},
        )
        self._api_key = api_key or os.getenv("OPENALEX_API_KEY")
        self._mailto = mailto or os.getenv("OPENALEX_MAILTO")
        self._store = store
        self._http = ReliableHttpClient(
            self._client,
            provider=self.name,
            store=store,
            policy=RetryPolicy(cache_ttl_seconds=3600, max_concurrency=4),
        )

    def _common_params(self) -> dict[str, str]:
        params: dict[str, str] = {}
        if self._api_key:
            params["api_key"] = self._api_key
        if self._mailto:
            params["mailto"] = self._mailto
        return params

    def plan_search_filter_execution(self, query: SearchQuery) -> dict[str, str]:
        execution = super().plan_search_filter_execution(query)
        if self._native_work_types(query.work_types):
            execution["work_types"] = "provider"
        return execution

    async def _get(
        self,
        path: str,
        params: dict[str, str] | None = None,
        *,
        operation: str = "get",
    ) -> dict[str, Any]:
        merged = self._common_params()
        merged.update(params or {})
        response = await self._http.get(path, params=merged, operation=operation)
        response.raise_for_status()
        return response.json()

    async def _get_work(
        self, identifier: str, *, operation: str = "resolve"
    ) -> dict[str, Any] | None:
        normalized_oa = normalize_openalex_id(identifier)
        normalized_doi = normalize_doi(identifier)
        if normalized_oa:
            target = normalized_oa
        elif normalized_doi:
            target = f"https://doi.org/{normalized_doi}"
        else:
            target = identifier.strip()
        try:
            return await self._get(
                f"/works/{quote(target, safe='')}",
                {"select": WORK_FIELDS},
                operation=operation,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise

    async def search(self, query: SearchQuery) -> ProviderBatch:
        params = {
            "search": query.text,
            "per_page": str(query.limit),
            "select": WORK_FIELDS,
        }
        title_filter = self._filter_search_value(query.title) if query.title else None
        filters: list[str] = []
        if query.year_from:
            filters.append(f"from_publication_date:{query.year_from}-01-01")
        if query.year_to:
            filters.append(f"to_publication_date:{query.year_to}-12-31")
        if query.author:
            filters.append(f"raw_author_name.search:{query.author}")
        if query.open_access is not None:
            filters.append(f"open_access.is_oa:{str(query.open_access).lower()}")
        if title_filter:
            # Generic search can rank an exact title outside our bounded recall
            # window. Push title matching upstream, while the service still
            # validates the original title against canonicalized results.
            filters.append(f"title.search:{title_filter}")
        if query.min_citations is not None and query.min_citations > 0:
            # OpenAlex supports strict numeric comparison. Subtract one to
            # preserve the neutral contract's inclusive minimum.
            filters.append(f"cited_by_count:>{query.min_citations - 1}")
        native_work_types = self._native_work_types(query.work_types)
        if native_work_types:
            filters.append(f"type:{'|'.join(native_work_types)}")
        if filters:
            params["filter"] = ",".join(filters)
        sort = {
            SearchSort.NEWEST: "publication_date:desc",
            SearchSort.OLDEST: "publication_date:asc",
            SearchSort.CITATIONS: "cited_by_count:desc",
        }.get(query.sort)
        if sort:
            params["sort"] = sort
        payload = await self._get("/works", params, operation="search")
        papers = [
            self._paper_from_work(work, rank=index + 1)
            for index, work in enumerate(payload["results"])
        ]
        total = payload.get("meta", {}).get("count")
        return ProviderBatch(
            papers=papers,
            total_available=total,
            truncated=bool(total is not None and total > len(papers)),
            filter_execution={
                field: "provider"
                for field, enabled in {
                    "text": True,
                    "year_from": query.year_from is not None,
                    "year_to": query.year_to is not None,
                    "author": query.author is not None,
                    "open_access": query.open_access is not None,
                    "title": bool(title_filter),
                    "min_citations": query.min_citations is not None,
                    "work_types": bool(native_work_types),
                    "sort": query.sort != SearchSort.RELEVANCE,
                }.items()
                if enabled
            },
        )

    async def resolve(self, identifier: str) -> Paper | None:
        work = await self._get_work(identifier, operation="resolve")
        return self._paper_from_work(work) if work else None

    async def related(self, query: RelatedQuery) -> ProviderBatch:
        if query.text is None:
            return ProviderBatch(filter_execution={"positive_identifiers": "unsupported"})
        params = {
            "search.semantic": query.text[:2000],
            "per_page": str(min(query.limit, 50)),
            "select": WORK_FIELDS,
        }
        filters: list[str] = []
        if query.year_from is not None:
            filters.append(f"from_publication_date:{query.year_from}-01-01")
        if query.year_to is not None:
            filters.append(f"to_publication_date:{query.year_to}-12-31")
        if query.open_access is not None:
            filters.append(f"open_access.is_oa:{str(query.open_access).lower()}")
        if query.include_work_types:
            filters.append(f"type:{'|'.join(query.include_work_types)}")
        if filters:
            params["filter"] = ",".join(filters)
        payload = await self._get("/works", params, operation="related")
        papers = [
            self._paper_from_work(
                work,
                rank=index + 1,
                score=work.get("relevance_score"),
                retrieval_method=RetrievalMethod.OPENALEX_SEMANTIC,
                retrieval_context={"input": "text"},
            )
            for index, work in enumerate(payload.get("results", []))
        ]
        total = payload.get("meta", {}).get("count")
        execution = {"text": "provider"}
        for field, enabled in {
            "year_from": query.year_from is not None,
            "year_to": query.year_to is not None,
            "open_access": query.open_access is not None,
            "include_work_types": bool(query.include_work_types),
        }.items():
            if enabled:
                execution[field] = "provider"
        if query.negative_identifiers:
            execution["negative_identifiers"] = "unsupported"
        return ProviderBatch(
            papers=papers,
            total_available=total,
            truncated=bool(total is not None and total > len(papers)),
            filter_execution=execution,
        )

    async def references(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        work = await self._get_work(identifier, operation="references")
        if not work:
            return ProviderBatch()
        reference_ids = [self._short_id(value) for value in work.get("referenced_works") or []]
        selected_ids = reference_ids[:limit]
        papers: list[Paper] = []
        resolved_reference_ids: set[str] = set()
        resume_index = 0
        checkpoint_key = make_checkpoint_key(
            self.name, "references", self._short_id(work["id"]), limit
        )
        if self._store:
            checkpoint = self._store.load_checkpoint(checkpoint_key)
            if checkpoint:
                try:
                    state = checkpoint.state
                    if (
                        checkpoint.provider != self.name
                        or checkpoint.operation != "references"
                        or state.get("version") != 2
                        or state.get("reference_ids") != selected_ids
                    ):
                        raise ValueError("stale OpenAlex reference checkpoint")
                    papers = [Paper.model_validate(item) for item in state["papers"]]
                    resolved_reference_ids = set(state["resolved_reference_ids"])
                    resume_index = int(checkpoint.cursor or len(selected_ids))
                except (KeyError, TypeError, ValueError):
                    # A stale/corrupt checkpoint must never poison future calls;
                    # discard it and replay this bounded request from the start.
                    self._store.delete_checkpoint(checkpoint_key)
                    papers = []
                    resolved_reference_ids = set()
                    resume_index = 0
        for chunk_start in range(resume_index, len(selected_ids), 100):
            chunk = selected_ids[chunk_start : chunk_start + 100]
            payload = await self._get(
                "/works",
                {
                    "filter": f"openalex:{'|'.join(chunk)}",
                    "per_page": str(len(chunk)),
                    "select": WORK_FIELDS,
                },
                operation="references",
            )
            items = payload.get("results", [])
            papers.extend(self._paper_from_work(item) for item in items)
            resolved_reference_ids.update(self._short_id(item["id"]) for item in items)
            if self._store:
                self._store.save_checkpoint(
                    checkpoint_key,
                    provider=self.name,
                    operation="references",
                    cursor=str(chunk_start + len(chunk)),
                    state={
                        "version": 2,
                        "reference_ids": selected_ids,
                        "papers": [paper.model_dump(mode="json") for paper in papers],
                        "resolved_reference_ids": sorted(resolved_reference_ids),
                    },
                )
        if self._store:
            # Checkpoints are crash-recovery state, not user-visible pagination
            # state. A normally completed call must replay from page one next time.
            self._store.delete_checkpoint(checkpoint_key)
        # The seed record can contain IDs that the batched Works endpoint no
        # longer exposes (deleted/merged/private records). Preserve each gap as
        # evidence so retrieved + unresolved accounts for the selected slice.
        seed_id = self._short_id(work["id"])
        missing_ids = set(selected_ids) - resolved_reference_ids
        unresolved = [
            UnresolvedReference(
                provider=self.name,
                seed_record_id=seed_id,
                ordinal=ordinal,
                raw={"openalex_id": reference_id},
                reason="referenced_openalex_work_not_returned",
                provenance=Provenance(
                    provider=self.name,
                    source_record_id=f"{seed_id}#reference-{ordinal}",
                    source_url=work.get("id"),
                ),
            )
            for ordinal, reference_id in enumerate(selected_ids, start=1)
            if reference_id in missing_ids
        ]
        return ProviderBatch(
            papers=papers,
            unresolved_references=unresolved,
            total_available=len(reference_ids),
            truncated=len(reference_ids) > len(selected_ids),
        )

    async def citations(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        work = await self._get_work(identifier, operation="citations")
        if not work:
            return ProviderBatch()
        work_id = self._short_id(work["id"])
        cursor = "*"
        papers: list[Paper] = []
        total: int | None = None
        next_cursor: str | None = None
        checkpoint_key = make_checkpoint_key(self.name, "citations", work_id, limit)
        if self._store:
            checkpoint = self._store.load_checkpoint(checkpoint_key)
            if checkpoint:
                try:
                    if (
                        checkpoint.provider != self.name
                        or checkpoint.operation != "citations"
                        or checkpoint.state.get("version") != 1
                    ):
                        raise ValueError("stale OpenAlex citation checkpoint")
                    papers = [Paper.model_validate(item) for item in checkpoint.state["papers"]]
                    total = checkpoint.state.get("total")
                    cursor = checkpoint.cursor or ""
                    next_cursor = checkpoint.cursor
                except (KeyError, TypeError, ValueError):
                    self._store.delete_checkpoint(checkpoint_key)
                    cursor = "*"
                    papers = []
                    total = None
                    next_cursor = None
        while len(papers) < limit:
            if not cursor:
                break
            page_size = min(100, limit - len(papers))
            payload = await self._get(
                "/works",
                {
                    "filter": f"cites:{work_id}",
                    "per_page": str(page_size),
                    "cursor": cursor,
                    "select": WORK_FIELDS,
                },
                operation="citations",
            )
            meta = payload.get("meta", {})
            total = meta.get("count", total)
            next_cursor = meta.get("next_cursor")
            items = payload.get("results", [])
            # OpenAlex can occasionally expose a work in its own ``cites``
            # result (observed for W3177828909). A work cannot form a useful
            # citation edge to itself, so discard that upstream anomaly here
            # and continue paging until the caller's logical limit is filled.
            papers.extend(
                self._paper_from_work(item)
                for item in items
                if self._short_id(item["id"]) != work_id
            )
            if self._store:
                # Persist only after the complete page has been normalized. On a
                # transport/mapping failure, the previous consistent page remains.
                self._store.save_checkpoint(
                    checkpoint_key,
                    provider=self.name,
                    operation="citations",
                    cursor=next_cursor,
                    state={
                        "version": 1,
                        "total": total,
                        "papers": [paper.model_dump(mode="json") for paper in papers],
                    },
                )
            if not items or not next_cursor:
                break
            # Cursors are opaque provider tokens; pass them back byte-for-byte
            # instead of deriving an offset from the number of returned works.
            cursor = next_cursor
        if self._store:
            self._store.delete_checkpoint(checkpoint_key)
        return ProviderBatch(
            papers=papers,
            total_available=total,
            truncated=bool((total is not None and total > len(papers)) or next_cursor),
            next_cursor=next_cursor,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @staticmethod
    def _short_id(value: str) -> str:
        return value.rstrip("/").rsplit("/", 1)[-1].upper()

    @staticmethod
    def _filter_search_value(value: str) -> str:
        """Remove OpenAlex filter delimiters without weakening local validation.

        Comma joins filters and pipe denotes alternatives in OpenAlex filter
        syntax. Convert only those delimiters to spaces; HTTPX handles URL
        encoding and the service checks the unmodified title after retrieval.
        """

        return " ".join(value.replace(",", " ").replace("|", " ").split())

    @staticmethod
    def _native_work_types(values: list[str]) -> list[str]:
        """Return values only when the whole neutral filter is safe upstream."""

        return list(values) if values and set(values) <= OPENALEX_WORK_TYPES else []

    def _paper_from_work(
        self,
        work: dict[str, Any],
        rank: int | None = None,
        *,
        score: float | None = None,
        retrieval_method: RetrievalMethod | None = None,
        retrieval_context: dict[str, Any] | None = None,
    ) -> Paper:
        retrieved_at = utc_now()
        openalex_id = self._short_id(work["id"])
        source_url = work.get("id")
        provenance = Provenance(
            provider=self.name,
            source_record_id=openalex_id,
            retrieved_at=retrieved_at,
            source_url=source_url,
        )
        identifiers = [
            IdentifierClaim(
                scheme=IdentifierScheme.OPENALEX,
                value=openalex_id,
                provenance=provenance,
            )
        ]
        id_values = dict(work.get("ids") or {})
        if work.get("doi"):
            id_values.setdefault("doi", work["doi"])
        scheme_map = {
            "doi": IdentifierScheme.DOI,
            "pmid": IdentifierScheme.PMID,
            "pmcid": IdentifierScheme.PMCID,
        }
        for key, scheme in scheme_map.items():
            raw_value = id_values.get(key)
            if not raw_value:
                continue
            if scheme == IdentifierScheme.DOI:
                value = normalize_doi(raw_value)
            else:
                value = str(raw_value).rstrip("/").rsplit("/", 1)[-1]
            if value:
                identifiers.append(
                    IdentifierClaim(scheme=scheme, value=value, provenance=provenance)
                )
        authors: list[Author] = []
        for authorship in work.get("authorships") or []:
            author_data = authorship.get("author") or {}
            name = author_data.get("display_name")
            if not name:
                continue
            provider_ids = {}
            if author_data.get("id"):
                provider_ids["openalex"] = self._short_id(author_data["id"])
            authors.append(
                Author(name=name, orcid=author_data.get("orcid"), provider_ids=provider_ids)
            )
        primary_location = work.get("primary_location") or {}
        source = primary_location.get("source") or {}
        publication_date = self._parse_date(work.get("publication_date"))
        title = work.get("title") or work.get("display_name") or f"Untitled {openalex_id}"
        cited_by_count = int(work.get("cited_by_count") or 0)
        open_access = work.get("open_access") or {}
        return Paper(
            record_id=f"openalex:{openalex_id}",
            title=title,
            abstract=self._restore_abstract(work.get("abstract_inverted_index")),
            authors=authors,
            publication_date=publication_date,
            publication_year=work.get("publication_year"),
            work_type=work.get("type"),
            venue=source.get("display_name"),
            fields_of_study=[
                topic["display_name"]
                for topic in work.get("topics") or []
                if topic.get("display_name")
            ],
            language=work.get("language"),
            open_access=open_access.get("is_oa"),
            landing_page_url=primary_location.get("landing_page_url") or source_url,
            pdf_url=primary_location.get("pdf_url"),
            identifiers=identifiers,
            citation_counts=[
                CitationCountClaim(
                    provider=self.name,
                    count=cited_by_count,
                    source_record_id=openalex_id,
                    retrieved_at=retrieved_at,
                )
            ],
            source_records=[
                SourceRecord(
                    provider=self.name,
                    source_record_id=openalex_id,
                    source_url=source_url,
                    provider_rank=rank,
                    provider_score=score,
                    retrieval_method=retrieval_method,
                    retrieval_context=retrieval_context or {},
                    retrieved_at=retrieved_at,
                )
            ],
            field_provenance={
                field: [provenance]
                for field in [
                    "title",
                    "abstract",
                    "authors",
                    "publication_date",
                    "work_type",
                    "venue",
                    "open_access",
                ]
            },
        )

    @staticmethod
    def _parse_date(value: str | None) -> date | None:
        if not value:
            return None
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None

    @staticmethod
    def _restore_abstract(index: dict[str, list[int]] | None) -> str | None:
        if not index:
            return None
        positions = [(position, word) for word, values in index.items() for position in values]
        return " ".join(word for _, word in sorted(positions))
