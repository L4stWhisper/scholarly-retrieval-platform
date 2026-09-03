"""ACL Anthology metadata adapter with transparent DBLP candidate discovery."""

from __future__ import annotations

from datetime import date
from typing import Any
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
from ..normalization import normalize_acl_anthology_id, normalize_doi
from ..reliability import ReliableHttpClient, RetryPolicy
from ..storage import SQLiteStore
from .base import ProviderCapabilities, ScholarlyProvider


class AclAnthologyProvider(ScholarlyProvider):
    """Search and resolve ACL Anthology papers without scraping result pages.

    ACL Anthology publishes authoritative per-paper MODS XML, but its website
    search is Google Custom Search rather than a public scholarly REST API. The
    adapter therefore uses DBLP's documented API only to discover candidate ACL
    IDs and accepts a result only after resolving it against ACL's official XML.
    This two-stage provenance is retained on every SourceRecord.
    """

    name = "acl_anthology"
    capabilities = ProviderCapabilities(
        keyword_search=True,
        advanced_search=True,
        resolve_id=True,
        references="none",
        citations="none",
        search_filter_execution={"text": "provider"},
        pagination={"search": "bounded_dblp_candidate_pool"},
        access_tier="public_no_key",
        terms_url="https://aclanthology.org/faq/api/",
        redistribution_policy="acl_material_license_and_dblp_discovery_terms_apply",
    )

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        discovery_client: httpx.AsyncClient | None = None,
        store: SQLiteStore | None = None,
    ) -> None:
        self._owns_client = client is None
        self._owns_discovery_client = discovery_client is None
        self._client = client or httpx.AsyncClient(
            base_url="https://aclanthology.org",
            timeout=httpx.Timeout(25.0),
            headers={"User-Agent": "scholarly-retrieval-platform/0.1"},
        )
        self._discovery_client = discovery_client or httpx.AsyncClient(
            base_url="https://dblp.org",
            timeout=httpx.Timeout(25.0),
            headers={"User-Agent": "scholarly-retrieval-platform/0.1"},
        )
        self._http = ReliableHttpClient(
            self._client,
            provider=self.name,
            store=store,
            policy=RetryPolicy(cache_ttl_seconds=86400, max_concurrency=2),
        )
        self._discovery_http = ReliableHttpClient(
            self._discovery_client,
            provider=f"{self.name}_dblp_discovery",
            store=store,
            policy=RetryPolicy(cache_ttl_seconds=3600, max_concurrency=1),
        )

    async def search(self, query: SearchQuery) -> ProviderBatch:
        # DBLP can mix non-ACL results into its relevance list. A bounded larger
        # pool improves recall while preserving a strict maximum of 100 records.
        candidate_pool_size = min(100, max(20, query.limit * 5))
        response = await self._discovery_http.get(
            "/search/publ/api",
            params={
                "q": query.text,
                "format": "json",
                "h": str(candidate_pool_size),
            },
            operation="acl_anthology_candidate_search",
        )
        response.raise_for_status()
        hits = response.json().get("result", {}).get("hits", {})
        raw_hits = hits.get("hit") or []
        if isinstance(raw_hits, dict):
            raw_hits = [raw_hits]

        candidates: list[tuple[str, int, dict[str, Any]]] = []
        seen: set[str] = set()
        for rank, hit in enumerate(raw_hits, start=1):
            info = hit.get("info") if isinstance(hit, dict) else None
            if not isinstance(info, dict):
                continue
            acl_id = self._candidate_acl_id(info)
            if acl_id is None or acl_id in seen:
                continue
            seen.add(acl_id)
            candidates.append((acl_id, rank, info))
            if len(candidates) >= query.limit:
                break

        papers = []
        for acl_id, rank, info in candidates:
            paper = await self._resolve_acl_id(
                acl_id,
                rank=rank,
                discovery_context={
                    "discovery_index": "dblp",
                    "discovery_record_id": info.get("key"),
                    "discovery_url": info.get("url"),
                },
            )
            if paper is not None:
                papers.append(paper)

        discovered_total = self._int(hits.get("@total"))
        # @total counts all DBLP results, not ACL-only records, so it must not be
        # exposed as ACL total_available. It is useful only as truncation evidence.
        truncated = bool(
            (discovered_total is not None and discovered_total > len(raw_hits))
            or len(candidates) > len(papers)
        )
        return ProviderBatch(
            papers=papers[: query.limit],
            total_available=None,
            truncated=truncated,
            filter_execution={"text": "provider"},
        )

    async def resolve(self, identifier: str) -> Paper | None:
        acl_id = normalize_acl_anthology_id(identifier)
        if acl_id is None:
            return None
        return await self._resolve_acl_id(acl_id)

    async def references(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        return ProviderBatch()

    async def citations(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        return ProviderBatch()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
        if self._owns_discovery_client:
            await self._discovery_client.aclose()

    async def _resolve_acl_id(
        self,
        acl_id: str,
        *,
        rank: int | None = None,
        discovery_context: dict[str, Any] | None = None,
    ) -> Paper | None:
        try:
            response = await self._http.get(
                f"/{acl_id}.xml",
                operation="acl_anthology_resolve",
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        return self._paper_from_mods(
            response.text,
            acl_id=acl_id,
            rank=rank,
            discovery_context=discovery_context,
        )

    def _paper_from_mods(
        self,
        xml: str,
        *,
        acl_id: str,
        rank: int | None = None,
        discovery_context: dict[str, Any] | None = None,
    ) -> Paper:
        # External entities are unnecessary in MODS metadata and create an XML
        # trust-boundary hazard if an upstream response is ever compromised.
        lowered = xml.casefold()
        if "<!doctype" in lowered or "<!entity" in lowered:
            raise ValueError("ACL Anthology XML contains a forbidden entity declaration")
        root = ElementTree.fromstring(xml)
        mods = root if self._local_name(root.tag) == "mods" else root.find("./{*}mods")
        if mods is None:
            raise ValueError("ACL Anthology response does not contain a MODS record")

        title = self._text(mods.find("./{*}titleInfo/{*}title"))
        if not title:
            raise ValueError("ACL Anthology MODS record has no title")
        source_url = f"https://aclanthology.org/{acl_id}/"
        provenance = Provenance(
            provider=self.name,
            source_record_id=acl_id,
            source_url=source_url,
        )
        authors = []
        for node in mods.findall("./{*}name"):
            roles = {
                self._text(role).casefold()
                for role in node.findall("./{*}role/{*}roleTerm")
                if self._text(role)
            }
            if "author" not in roles:
                continue
            given = self._named_part(node, "given")
            family = self._named_part(node, "family")
            name = " ".join(part for part in (given, family) if part).strip()
            if not name:
                name = " ".join(
                    self._text(part) for part in node.findall("./{*}namePart") if self._text(part)
                ).strip()
            if name:
                orcid = next(
                    (
                        self._text(item)
                        for item in node.findall("./{*}nameIdentifier")
                        if str(item.attrib.get("type") or "").casefold() == "orcid"
                    ),
                    None,
                )
                authors.append(Author(name=name, orcid=orcid or None))

        issued = self._text(mods.find("./{*}originInfo/{*}dateIssued"))
        publication_date = self._date(issued)
        venue = self._text(mods.find("./{*}relatedItem[@type='host']/{*}titleInfo/{*}title"))
        language = self._text(mods.find("./{*}language/{*}languageTerm")) or None
        identifiers = [
            IdentifierClaim(
                scheme=IdentifierScheme.ACL_ANTHOLOGY,
                value=acl_id,
                provenance=provenance,
            )
        ]
        for node in mods.findall("./{*}identifier"):
            if str(node.attrib.get("type") or "").casefold() != "doi":
                continue
            doi = normalize_doi(self._text(node))
            if doi:
                identifiers.append(
                    IdentifierClaim(
                        scheme=IdentifierScheme.DOI,
                        value=doi,
                        provenance=provenance,
                    )
                )

        pdf_url = f"https://aclanthology.org/{acl_id}.pdf"
        retrieval_context = {
            key: value for key, value in (discovery_context or {}).items() if value is not None
        }
        field_values = {
            "title": title,
            "authors": authors,
            "publication_date": publication_date,
            "publication_year": publication_date.year if publication_date else None,
            "venue": venue or None,
            "language": language,
            "open_access": True,
            "landing_page_url": source_url,
            "pdf_url": pdf_url,
        }
        return Paper(
            record_id=f"acl_anthology:{acl_id}",
            title=title,
            authors=authors,
            publication_date=publication_date,
            publication_year=publication_date.year if publication_date else None,
            work_type="conference-paper",
            venue=venue or None,
            language=language,
            open_access=True,
            landing_page_url=source_url,
            pdf_url=pdf_url,
            identifiers=identifiers,
            source_records=[
                SourceRecord(
                    provider=self.name,
                    source_record_id=acl_id,
                    source_url=source_url,
                    provider_rank=rank,
                    retrieval_context=retrieval_context,
                )
            ],
            field_provenance={
                field: [provenance] for field, value in field_values.items() if value is not None
            },
        )

    @staticmethod
    def _candidate_acl_id(info: dict[str, Any]) -> str | None:
        original_values = info.get("ee") or []
        raw_values = (
            list(original_values) if isinstance(original_values, list) else [original_values]
        )
        raw_values.append(info.get("doi"))
        for raw_value in raw_values:
            if isinstance(raw_value, dict):
                raw_value = raw_value.get("text") or raw_value.get("url")
            acl_id = normalize_acl_anthology_id(str(raw_value or ""))
            if acl_id:
                return acl_id
        return None

    @staticmethod
    def _text(node: ElementTree.Element | None) -> str:
        return " ".join("".join(node.itertext()).split()) if node is not None else ""

    @classmethod
    def _named_part(cls, node: ElementTree.Element, part_type: str) -> str:
        return next(
            (
                cls._text(part)
                for part in node.findall("./{*}namePart")
                if str(part.attrib.get("type") or "").casefold() == part_type
            ),
            "",
        )

    @staticmethod
    def _local_name(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    @staticmethod
    def _date(value: str) -> date | None:
        if len(value) < 4 or not value[:4].isdigit():
            return None
        year = int(value[:4])
        month = int(value[5:7]) if len(value) >= 7 and value[5:7].isdigit() else 1
        day = int(value[8:10]) if len(value) >= 10 and value[8:10].isdigit() else 1
        try:
            return date(year, month, day)
        except ValueError:
            return date(year, 1, 1)

    @staticmethod
    def _int(value: Any) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None
