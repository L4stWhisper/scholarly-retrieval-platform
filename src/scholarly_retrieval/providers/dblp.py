"""DBLP computer-science bibliography search adapter."""

from __future__ import annotations

from datetime import date
from typing import Any
from urllib.parse import quote
from xml.etree import ElementTree

import httpx

from ..models import (
    Author,
    IdentifierClaim,
    IdentifierScheme,
    Paper,
    Provenance,
    ProviderBatch,
    SearchQuery,
    SourceRecord,
)
from ..normalization import normalize_doi
from ..reliability import ReliableHttpClient, RetryPolicy
from ..storage import SQLiteStore
from .base import ProviderCapabilities, ScholarlyProvider


class DblpProvider(ScholarlyProvider):
    name = "dblp"
    capabilities = ProviderCapabilities(
        keyword_search=True,
        advanced_search=False,
        resolve_id=True,
        references="none",
        citations="none",
        search_filter_execution={"text": "provider"},
        pagination={"search": "offset"},
        access_tier="public_no_key",
        terms_url="https://dblp.org/faq/How+to+use+the+dblp+search+API.html",
        redistribution_policy="dblp_open_data_terms_apply",
    )

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        store: SQLiteStore | None = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url="https://dblp.org",
            timeout=httpx.Timeout(20.0),
            headers={"User-Agent": "scholarly-retrieval-platform/0.1"},
        )
        self._http = ReliableHttpClient(
            self._client,
            provider=self.name,
            store=store,
            policy=RetryPolicy(cache_ttl_seconds=3600, max_concurrency=2),
        )

    async def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        response = await self._http.get(path, params=params, operation="dblp_get")
        response.raise_for_status()
        return response.json()

    async def search(self, query: SearchQuery) -> ProviderBatch:
        payload = await self._get(
            "/search/publ/api",
            {"q": query.text, "format": "json", "h": str(query.limit)},
        )
        hits = payload.get("result", {}).get("hits", {})
        raw_hits = hits.get("hit") or []
        if isinstance(raw_hits, dict):
            raw_hits = [raw_hits]
        papers = [
            self._paper_from_info(hit.get("info") or {}, rank=index + 1)
            for index, hit in enumerate(raw_hits)
            if (hit.get("info") or {}).get("title")
        ]
        total = self._int(hits.get("@total"))
        return ProviderBatch(
            papers=papers,
            total_available=total,
            truncated=bool(total is not None and total > len(papers)),
            filter_execution={"text": "provider"},
        )

    async def resolve(self, identifier: str) -> Paper | None:
        doi = normalize_doi(identifier)
        if doi:
            batch = await self.search(SearchQuery(text=f"doi:{doi}", limit=10))
            return next(
                (
                    paper
                    for paper in batch.papers
                    if any(
                        claim.scheme == IdentifierScheme.DOI and claim.value == doi
                        for claim in paper.identifiers
                    )
                ),
                None,
            )
        normalized = identifier.strip()
        if normalized.casefold().startswith("dblp:"):
            key = normalized[5:].strip("/")
            if not key:
                return None
            try:
                # DBLP's search endpoint supports JSON, while an individual record is
                # exported as XML. Keeping these paths separate avoids interpreting an
                # HTML 404 response as JSON and also preserves the canonical DBLP key.
                response = await self._http.get(
                    f"/rec/{quote(key, safe='/')}.xml",
                    operation="dblp_resolve",
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    return None
                raise
            return self._paper_from_xml(response.text, expected_key=key)
        return None

    async def references(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        return ProviderBatch()

    async def citations(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        return ProviderBatch()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _paper_from_info(self, info: dict[str, Any], rank: int | None = None) -> Paper:
        key = str(info.get("key") or info.get("url") or info.get("@id") or "unknown")
        title = str(info.get("title") or key).rstrip(".").strip()
        source_url = info.get("url") or f"https://dblp.org/rec/{key}"
        provenance = Provenance(provider=self.name, source_record_id=key, source_url=source_url)
        raw_authors = (info.get("authors") or {}).get("author") or []
        if isinstance(raw_authors, (str, dict)):
            raw_authors = [raw_authors]
        authors = []
        for item in raw_authors:
            name = item.get("text") if isinstance(item, dict) else item
            if name:
                authors.append(Author(name=str(name)))
        year = self._int(info.get("year"))
        doi = normalize_doi(str(info.get("doi") or ""))
        identifiers = [
            IdentifierClaim(scheme=IdentifierScheme.DBLP, value=key, provenance=provenance)
        ]
        if doi:
            identifiers.append(
                IdentifierClaim(scheme=IdentifierScheme.DOI, value=doi, provenance=provenance)
            )
        return Paper(
            record_id=f"dblp:{key}",
            title=title,
            authors=authors,
            publication_date=date(year, 1, 1) if year else None,
            publication_year=year,
            work_type=str(info.get("type") or "").casefold() or None,
            venue=info.get("venue"),
            open_access=info.get("access") == "open" if info.get("access") else None,
            landing_page_url=source_url,
            identifiers=identifiers,
            source_records=[
                SourceRecord(
                    provider=self.name,
                    source_record_id=key,
                    source_url=source_url,
                    provider_rank=rank,
                )
            ],
            field_provenance={
                field: [provenance]
                for field, value in {
                    "title": title,
                    "authors": authors,
                    "publication_year": year,
                    "venue": info.get("venue"),
                    "work_type": info.get("type"),
                }.items()
                if value not in (None, [], "")
            },
        )

    def _paper_from_xml(self, xml: str, *, expected_key: str) -> Paper | None:
        """Map DBLP's single-record XML export without resolving XML entities."""

        # ElementTree does not fetch external DTDs, but an explicit entity declaration
        # is never needed for a DBLP record and should not enter this trust boundary.
        if "<!ENTITY" in xml.upper():
            raise ValueError("DBLP record XML contains a forbidden entity declaration")
        root = ElementTree.fromstring(xml)
        record = root if root.tag != "dblp" else next(iter(root), None)
        if record is None or record.attrib.get("key") != expected_key:
            return None

        def values(name: str) -> list[ElementTree.Element]:
            return [child for child in record if child.tag == name]

        def text(name: str) -> str | None:
            elements = values(name)
            if not elements:
                return None
            value = "".join(elements[0].itertext()).strip()
            return value or None

        authors = [
            {"text": "".join(author.itertext()).strip(), "@pid": author.attrib.get("pid")}
            for author in values("author") + values("editor")
            if "".join(author.itertext()).strip()
        ]
        electronic_editions = [
            "".join(element.itertext()).strip() for element in values("ee")
        ]
        doi = text("doi")
        if not doi:
            doi = next(
                (
                    candidate
                    for candidate in electronic_editions
                    if normalize_doi(candidate) is not None
                ),
                None,
            )
        info: dict[str, Any] = {
            "authors": {"author": authors},
            "title": text("title"),
            "venue": text("booktitle") or text("journal"),
            "year": text("year"),
            "type": record.tag,
            "access": (
                "open"
                if any(element.attrib.get("type") == "oa" for element in values("ee"))
                else None
            ),
            "key": expected_key,
            "doi": doi,
            # The DBLP record page is stable; the XML's <url> is often an internal
            # bibliography anchor and should not replace this public landing URL.
            "url": f"https://dblp.org/rec/{expected_key}",
        }
        return self._paper_from_info(info)

    @staticmethod
    def _int(value: Any) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None
