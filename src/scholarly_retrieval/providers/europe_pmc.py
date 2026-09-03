"""Europe PMC adapter for biomedical search and citation traversal."""

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
    SearchQuery,
    SourceRecord,
    UnresolvedReference,
    utc_now,
)
from ..normalization import normalize_doi
from ..reliability import ReliableHttpClient, RetryPolicy
from ..storage import SQLiteStore
from .base import ProviderCapabilities, ScholarlyProvider


class EuropePmcProvider(ScholarlyProvider):
    """Open biomedical metadata, matched references, and citing publications."""

    name = "europe_pmc"
    capabilities = ProviderCapabilities(
        keyword_search=True,
        advanced_search=True,
        resolve_id=True,
        references="list",
        citations="list",
        related=False,
        fulltext=True,
        search_filter_execution={
            "text": "provider",
            "year_from": "provider",
            "year_to": "provider",
            "author": "provider",
            "open_access": "provider",
        },
        pagination={
            "search": "opaque_cursor",
            "references": "page_number",
            "citations": "page_number",
        },
        access_tier="public_rate_limited",
        credential_variables=["EUROPE_PMC_EMAIL"],
        terms_url="https://europepmc.org/RestfulWebService",
        redistribution_policy="metadata_open_fulltext_license_per_article",
    )

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        email: str | None = None,
        store: SQLiteStore | None = None,
    ) -> None:
        self._owns_client = client is None
        self._email = email or os.getenv("EUROPE_PMC_EMAIL")
        self._client = client or httpx.AsyncClient(
            base_url="https://www.ebi.ac.uk/europepmc/webservices/rest",
            timeout=httpx.Timeout(30.0),
            headers={"User-Agent": "scholarly-retrieval-platform/0.1"},
        )
        self._http = ReliableHttpClient(
            self._client,
            provider=self.name,
            store=store,
            policy=RetryPolicy(max_attempts=3, min_interval_seconds=0.2, max_concurrency=2),
        )

    async def _get(
        self,
        path: str,
        params: dict[str, str] | None = None,
        *,
        operation: str,
    ) -> dict[str, Any]:
        merged = dict(params or {})
        merged["format"] = "json"
        if self._email:
            merged["email"] = self._email
        response = await self._http.get(path, params=merged, operation=operation)
        response.raise_for_status()
        return response.json()

    async def search(self, query: SearchQuery) -> ProviderBatch:
        clauses = [f"({query.text})"]
        execution = {"text": "provider"}
        if query.author:
            clauses.append(f'AUTH:"{self._escape(query.author)}"')
            execution["author"] = "provider"
        if query.year_from is not None or query.year_to is not None:
            start = query.year_from or 1000
            end = query.year_to or 3000
            clauses.append(f"FIRST_PDATE:[{start}-01-01 TO {end}-12-31]")
            if query.year_from is not None:
                execution["year_from"] = "provider"
            if query.year_to is not None:
                execution["year_to"] = "provider"
        if query.open_access is not None:
            clauses.append(f"OPEN_ACCESS:{'Y' if query.open_access else 'N'}")
            execution["open_access"] = "provider"
        payload = await self._get(
            "/search",
            {
                "query": " AND ".join(clauses),
                "resultType": "core",
                "cursorMark": "*",
                "pageSize": str(query.limit),
            },
            operation="search",
        )
        items = self._as_list((payload.get("resultList") or {}).get("result"))
        papers = [self._paper_from_result(item, rank=index + 1) for index, item in enumerate(items)]
        total = self._int(payload.get("hitCount"))
        next_cursor = payload.get("nextCursorMark")
        return ProviderBatch(
            papers=papers,
            total_available=total,
            truncated=bool(total is not None and total > len(papers)),
            next_cursor=str(next_cursor) if next_cursor else None,
            filter_execution=execution,
        )

    async def resolve(self, identifier: str) -> Paper | None:
        direct = self._direct_source_id(identifier)
        if direct:
            source, record_id = direct
            try:
                payload = await self._get(
                    f"/article/{quote(source, safe='')}/{quote(record_id, safe='')}",
                    {"resultType": "core"},
                    operation="resolve",
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    return None
                raise
            item = payload.get("result") or payload
            return self._paper_from_result(item) if item.get("id") else None

        doi = normalize_doi(identifier)
        if not doi:
            return None
        payload = await self._get(
            "/search",
            {
                "query": f'DOI:"{self._escape(doi)}"',
                "resultType": "core",
                "pageSize": "1",
            },
            operation="resolve",
        )
        items = self._as_list((payload.get("resultList") or {}).get("result"))
        return self._paper_from_result(items[0]) if items else None

    async def references(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        return await self._relations(identifier, relation="references", limit=limit)

    async def citations(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        return await self._relations(identifier, relation="citations", limit=limit)

    async def _relations(
        self,
        identifier: str,
        *,
        relation: str,
        limit: int,
    ) -> ProviderBatch:
        source_id = self._direct_source_id(identifier)
        if source_id is None:
            seed = await self.resolve(identifier)
            if seed is None:
                return ProviderBatch()
            source = seed.source_records[0].source_record_id.split(":", 1)[0]
            record_id = seed.source_records[0].source_record_id.split(":", 1)[1]
        else:
            source, record_id = source_id
        payload = await self._get(
            f"/{quote(source, safe='')}/{quote(record_id, safe='')}/{relation}",
            {"page": "1", "pageSize": str(min(limit, 1000))},
            operation=relation,
        )
        list_key = "referenceList" if relation == "references" else "citationList"
        item_key = "reference" if relation == "references" else "citation"
        items = self._as_list((payload.get(list_key) or {}).get(item_key))[:limit]
        papers: list[Paper] = []
        unresolved: list[UnresolvedReference] = []
        for ordinal, item in enumerate(items, start=1):
            if item.get("id") and item.get("source"):
                papers.append(self._paper_from_relation(item))
            elif relation == "references":
                provenance = Provenance(
                    provider=self.name,
                    source_record_id=f"{source}:{record_id}#reference-{ordinal}",
                    source_url=f"https://europepmc.org/article/{source}/{record_id}",
                )
                unresolved.append(
                    UnresolvedReference(
                        provider=self.name,
                        seed_record_id=f"{source}:{record_id}",
                        ordinal=ordinal,
                        raw=item,
                        title=item.get("title"),
                        author=item.get("authorString"),
                        publication_year=self._int(item.get("pubYear")),
                        reason="missing_strong_identifier",
                        provenance=provenance,
                    )
                )
        total = self._int(payload.get("hitCount"))
        retrieved = len(papers) + len(unresolved)
        return ProviderBatch(
            papers=papers,
            unresolved_references=unresolved,
            total_available=total,
            truncated=bool(total is not None and total > retrieved),
            next_cursor="2" if total is not None and total > retrieved else None,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _paper_from_result(self, item: dict[str, Any], rank: int | None = None) -> Paper:
        source = str(item.get("source") or "UNKNOWN").upper()
        record_id = str(item.get("id") or item.get("pmid") or item.get("pmcid") or "UNKNOWN")
        source_record_id = f"{source}:{record_id}"
        source_url = f"https://europepmc.org/article/{source}/{record_id}"
        retrieved_at = utc_now()
        provenance = Provenance(
            provider=self.name,
            source_record_id=source_record_id,
            source_url=source_url,
            retrieved_at=retrieved_at,
        )
        identifiers = self._identifier_claims(item, provenance, source, record_id)
        authors = []
        author_list = item.get("authorList") or {}
        for author in self._as_list(author_list.get("author")):
            name = author.get("fullName") or " ".join(
                value for value in [author.get("firstName"), author.get("lastName")] if value
            )
            if name:
                authors.append(
                    Author(
                        name=name,
                        provider_ids=(
                            {"europe_pmc": str(author["authorId"])}
                            if author.get("authorId")
                            else {}
                        ),
                    )
                )
        publication_types = self._as_list((item.get("pubTypeList") or {}).get("pubType"))
        journal = ((item.get("journalInfo") or {}).get("journal") or {}).get("title")
        pdf_url = None
        landing_page = source_url
        for link in self._as_list((item.get("fullTextUrlList") or {}).get("fullTextUrl")):
            url = link.get("url")
            if not url:
                continue
            if str(link.get("documentStyle") or "").casefold() == "pdf":
                pdf_url = url
            elif landing_page == source_url:
                landing_page = url
        citation_count = self._int(item.get("citedByCount"))
        return Paper(
            record_id=f"europe_pmc:{source}:{record_id}",
            title=item.get("title") or f"Untitled {source}:{record_id}",
            abstract=item.get("abstractText"),
            authors=authors,
            publication_date=self._date(item.get("firstPublicationDate")),
            publication_year=self._int(item.get("pubYear")),
            work_type=str(publication_types[0]) if publication_types else None,
            venue=journal or item.get("journalTitle") or item.get("journalAbbreviation"),
            language=item.get("language"),
            open_access=self._yes_no(item.get("isOpenAccess")),
            landing_page_url=landing_page,
            pdf_url=pdf_url,
            identifiers=identifiers,
            citation_counts=(
                [
                    CitationCountClaim(
                        provider=self.name,
                        count=citation_count,
                        source_record_id=source_record_id,
                        retrieved_at=retrieved_at,
                    )
                ]
                if citation_count is not None
                else []
            ),
            source_records=[
                SourceRecord(
                    provider=self.name,
                    source_record_id=source_record_id,
                    source_url=source_url,
                    provider_rank=rank,
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

    def _paper_from_relation(self, item: dict[str, Any]) -> Paper:
        source = str(item["source"]).upper()
        record_id = str(item["id"])
        source_record_id = f"{source}:{record_id}"
        source_url = f"https://europepmc.org/article/{source}/{record_id}"
        provenance = Provenance(
            provider=self.name,
            source_record_id=source_record_id,
            source_url=source_url,
        )
        citation_count = self._int(item.get("citedByCount"))
        return Paper(
            record_id=f"europe_pmc:{source}:{record_id}",
            title=item.get("title") or f"Untitled {source}:{record_id}",
            authors=(
                [Author(name=item["authorString"])] if item.get("authorString") else []
            ),
            publication_year=self._int(item.get("pubYear")),
            work_type=item.get("citationType"),
            venue=item.get("journalAbbreviation"),
            landing_page_url=source_url,
            identifiers=self._identifier_claims(item, provenance, source, record_id),
            citation_counts=(
                [
                    CitationCountClaim(
                        provider=self.name,
                        count=citation_count,
                        source_record_id=source_record_id,
                    )
                ]
                if citation_count is not None
                else []
            ),
            source_records=[
                SourceRecord(
                    provider=self.name,
                    source_record_id=source_record_id,
                    source_url=source_url,
                )
            ],
        )

    @staticmethod
    def _identifier_claims(
        item: dict[str, Any],
        provenance: Provenance,
        source: str,
        record_id: str,
    ) -> list[IdentifierClaim]:
        values: list[tuple[IdentifierScheme, str | None]] = [
            (IdentifierScheme.DOI, normalize_doi(str(item.get("doi") or ""))),
            (IdentifierScheme.PMID, str(item.get("pmid") or "") or None),
            (IdentifierScheme.PMCID, str(item.get("pmcid") or "") or None),
        ]
        has_pmid = any(
            scheme == IdentifierScheme.PMID and value for scheme, value in values
        )
        has_pmcid = any(
            scheme == IdentifierScheme.PMCID and value for scheme, value in values
        )
        if source == "MED" and not has_pmid:
            values.append((IdentifierScheme.PMID, record_id))
        if source == "PMC" and not has_pmcid:
            values.append((IdentifierScheme.PMCID, record_id))
        return [
            IdentifierClaim(scheme=scheme, value=value, provenance=provenance)
            for scheme, value in values
            if value
        ]

    @staticmethod
    def _direct_source_id(identifier: str) -> tuple[str, str] | None:
        raw = identifier.strip()
        if raw.casefold().startswith("europe_pmc:"):
            parts = raw.split(":", 2)
            if len(parts) == 3 and parts[1] and parts[2]:
                return parts[1].upper(), parts[2]
        upper = raw.upper()
        if upper.startswith("PMID:") and upper[5:].isdigit():
            return "MED", upper[5:]
        if raw.isdigit():
            return "MED", raw
        if upper.startswith("PMCID:"):
            upper = upper[6:]
        if upper.startswith("PMC") and upper[3:].isdigit():
            return "PMC", upper
        return None

    @staticmethod
    def _escape(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')

    @staticmethod
    def _as_list(value: Any) -> list[dict[str, Any]]:
        if value is None:
            return []
        return value if isinstance(value, list) else [value]

    @staticmethod
    def _int(value: Any) -> int | None:
        try:
            return int(value) if value is not None and value != "" else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _date(value: Any) -> date | None:
        if not value:
            return None
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            return None

    @staticmethod
    def _yes_no(value: Any) -> bool | None:
        normalized = str(value).strip().casefold()
        if normalized in {"y", "yes", "true", "1"}:
            return True
        if normalized in {"n", "no", "false", "0"}:
            return False
        return None
