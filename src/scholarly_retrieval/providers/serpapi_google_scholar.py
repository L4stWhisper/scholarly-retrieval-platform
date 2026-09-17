"""Optional, user-authorized Google Scholar access through SerpApi."""

from __future__ import annotations

import hashlib
import os
import re
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx

from ..models import (
    Author,
    CitationCountClaim,
    IdentifierClaim,
    IdentifierScheme,
    Paper,
    Provenance,
    ProviderBatch,
    RunStatus,
    SearchQuery,
    SourceRecord,
)
from ..normalization import normalize_arxiv_id, normalize_doi
from ..reliability import ReliableHttpClient, RetryPolicy
from ..storage import SQLiteStore
from .base import ProviderCapabilities, ScholarlyProvider

DOI_PATTERN = re.compile(r"\b10\.\d{4,9}/[^\s<>\]}]+", re.IGNORECASE)
YEAR_PATTERN = re.compile(r"\b(1\d{3}|2\d{3})\b")
ARXIV_URL_PATTERN = re.compile(
    r"(?:arxiv:\s*|arxiv\.org/(?:abs|pdf)/)(\d{4}\.\d{4,5}(?:v\d+)?)",
    re.IGNORECASE,
)


class SerpApiGoogleScholarProvider(ScholarlyProvider):
    """Paid/authorized adapter; it never scrapes scholar.google.com directly."""

    name = "google_scholar_serpapi"
    capabilities = ProviderCapabilities(
        keyword_search=True,
        advanced_search=True,
        resolve_id=True,
        references="none",
        citations="list",
        search_filter_execution={
            "text": "provider",
            "year_from": "provider",
            "year_to": "provider",
            "author": "provider",
            "venue": "provider",
            "open_access": "local",
        },
        pagination={"search": "start_offset", "citations": "start_offset"},
        access_tier="paid_api_key",
        credential_variables=["SERPAPI_API_KEY"],
        terms_url="https://serpapi.com/legal",
        redistribution_policy="serpapi_terms_and_upstream_rights_apply",
    )

    def __init__(
        self,
        *,
        api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
        store: SQLiteStore | None = None,
        citation_rounds: int | None = None,
        discovery_pages: int = 12,
    ) -> None:
        key = api_key or os.getenv("SERPAPI_API_KEY")
        if not key:
            raise ValueError("SERPAPI_API_KEY is required for Google Scholar")
        self._api_key = key
        if citation_rounds is None:
            citation_rounds = int(os.getenv("SCHOLAR_GOOGLE_CITATION_ROUNDS", "4"))
        if citation_rounds < 2 or discovery_pages < 1:
            raise ValueError("citation_rounds must be >= 2 and discovery_pages >= 1")
        self._citation_rounds = citation_rounds
        self._discovery_pages = discovery_pages
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url="https://serpapi.com", timeout=httpx.Timeout(30.0)
        )
        self._http = ReliableHttpClient(
            self._client,
            provider=self.name,
            store=store,
            policy=RetryPolicy(cache_ttl_seconds=3600, max_concurrency=1),
        )

    async def _query(self, params: dict[str, str], operation: str) -> dict[str, Any]:
        response = await self._http.get(
            "/search.json",
            params={"engine": "google_scholar", "api_key": self._api_key, **params},
            operation=operation,
            use_cache=params.get("no_cache") != "true",
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("error"):
            # SerpApi also represents a legitimate empty search as an error.
            # Only recognize this exact case; quota/auth failures must propagate.
            if payload["error"] == "Google hasn't returned any results for this query.":
                return {**payload, "organic_results": []}
            raise ValueError("SerpApi Google Scholar returned an API error")
        return payload

    def traversal_identifier(self, paper: Paper, fallback: str) -> str:
        # A versions cluster is not necessarily the identifier of its Cited by
        # list. Only use cites IDs explicitly returned by the upstream service.
        cites = list(
            dict.fromkeys(
                str(record.retrieval_context["cites_id"])
                for record in paper.source_records
                if record.provider == self.name and record.retrieval_context.get("cites_id")
            )
        )
        return "google_scholar:" + ",".join(cites) if cites else fallback

    @staticmethod
    def _same_seed(seed: Paper, candidate: Paper) -> bool:
        def title_key(title: str) -> str:
            # Citation-only entries sometimes append the publication year.
            title = re.sub(r"[,\.\s]+(?:19|20)\d{2}[.\s]*$", "", title)
            return re.sub(r"[^\w]", "", title.casefold())

        def surname(name: str) -> str:
            return (name.split(",", 1)[0] if "," in name else name.split()[-1]).strip().casefold()

        strong = {IdentifierScheme.DOI, IdentifierScheme.ARXIV}
        seed_ids = {(c.scheme, c.value.casefold()) for c in seed.identifiers if c.scheme in strong}
        ids = {(c.scheme, c.value.casefold()) for c in candidate.identifiers if c.scheme in strong}
        conflict = any(
            {v for s, v in seed_ids if s == scheme}
            and {v for s, v in ids if s == scheme}
            and not ({v for s, v in seed_ids if s == scheme} & {v for s, v in ids if s == scheme})
            for scheme in strong
        )
        surnames = {surname(a.name) for a in seed.authors if a.name.split()}
        compatible = (
            title_key(candidate.title) == title_key(seed.title)
            and (
                not candidate.publication_year
                or not seed.publication_year
                or abs(candidate.publication_year - seed.publication_year) <= 1
            )
            and any(surname(a.name) in surnames for a in candidate.authors if a.name.split())
        )
        return not conflict and bool(seed_ids & ids or compatible)

    @staticmethod
    def _next_params(payload: dict[str, Any], current: dict[str, str]) -> dict[str, str] | None:
        link = (payload.get("serpapi_pagination") or {}).get("next")
        if not link:
            return None
        url = urlsplit(link)
        # Never send the API key to an upstream-supplied arbitrary URL. Follow
        # official paging parameters through our fixed SerpApi endpoint only.
        if url.scheme != "https" or url.netloc != "serpapi.com" or url.path != "/search.json":
            raise ValueError("invalid SerpApi pagination endpoint")
        pairs = dict(parse_qsl(url.query))
        if pairs.get("engine", "google_scholar") != "google_scholar":
            raise ValueError("pagination changed engine")
        for key in ("q", "cites", "cluster"):
            if key in pairs and pairs[key] != current.get(key):
                raise ValueError("pagination changed seed query")
        if not pairs.get("start", "").isdigit() or int(pairs["start"]) <= int(
            current.get("start", "0")
        ):
            raise ValueError("pagination failed to advance")
        allowed = {
            "start",
            "num",
            "hl",
            "gl",
            "as_sdt",
            "as_vis",
            "filter",
            "as_ylo",
            "as_yhi",
            "scipsc",
        }
        return {**current, **{k: v for k, v in pairs.items() if k in allowed}, "no_cache": "true"}

    async def _discover(self, seed: Paper, extra_clusters: list[str] | None = None) -> Paper:
        """Bounded traversal of verified search matches and their All Versions.

        Search results are candidates, not proof of identity. Unrelated works
        must never contribute a cites ID, even if returned for an exact title.
        """
        result = seed.model_copy(deep=True)
        # Include citation-only and similar entries: these may carry a second
        # Cited by list even when the regular search hides duplicate versions.
        discovery_options = {"num": "20", "no_cache": "true", "filter": "0", "as_vis": "0"}
        queue = [
            {"q": f'"{seed.title}"', **discovery_options},
            {"q": f'intitle:"{seed.title}"', **discovery_options},
        ]
        queue += [
            {"q": f'"{claim.value}"', **discovery_options}
            for claim in seed.identifiers
            if claim.scheme in {IdentifierScheme.ARXIV, IdentifierScheme.DOI}
        ]
        queue += [{"cluster": c, **discovery_options} for c in extra_clusters or []]
        seen_queries: set[tuple] = set()
        seen_records = {
            (r.source_record_id, str(r.retrieval_context)) for r in result.source_records
        }
        errors: list[str] = []
        verified_clusters: set[str] = set()

        def enqueue_versions(paper: Paper) -> None:
            for record in paper.source_records:
                cluster = record.retrieval_context.get("cluster_id")
                if cluster:
                    queue.append({"cluster": str(cluster), **discovery_options})

        enqueue_versions(seed)
        while queue and len(seen_queries) < self._discovery_pages:
            params = queue.pop(0)
            key = tuple(sorted(params.items()))
            if key in seen_queries:
                continue
            seen_queries.add(key)
            try:
                payload = await self._query(params, "resolve")
                for item in payload.get("organic_results", []):
                    candidate = self._paper(item)
                    if not self._same_seed(seed, candidate):
                        continue
                    if params.get("cluster"):
                        verified_clusters.add(params["cluster"])
                        for record in candidate.source_records:
                            record.retrieval_context["lookup_cluster_id"] = params["cluster"]
                    enqueue_versions(candidate)
                    for record in candidate.source_records:
                        identity = (record.source_record_id, str(record.retrieval_context))
                        if identity not in seen_records:
                            seen_records.add(identity)
                            result.source_records.append(record)
                    known = {(c.scheme, c.value) for c in result.identifiers}
                    result.identifiers.extend(
                        c for c in candidate.identifiers if (c.scheme, c.value) not in known
                    )
                following = self._next_params(payload, params)
                if following:
                    queue.append(following)
            except (httpx.HTTPError, ValueError) as exc:
                errors.append(type(exc).__name__)
                # Avoid spending more quota after an authentication/quota error.
                break
        pending = any(tuple(sorted(p.items())) not in seen_queries for p in queue)
        for record in result.source_records:
            record.retrieval_context["discovery"] = {
                "requests": len(seen_queries),
                "budget_exhausted": pending,
                "errors": errors,
                "unverified_requested_clusters": sorted(
                    set(extra_clusters or []) - verified_clusters
                ),
                "scope": "discoverable_verified_matches_not_entire_google_index",
            }
        return result

    async def resolve_seed(self, paper: Paper) -> Paper | None:
        result = await self._discover(paper)
        return result if any(r.provider == self.name for r in result.source_records) else None

    async def search(self, query: SearchQuery) -> ProviderBatch:
        terms = [query.text]
        if query.author:
            terms.append(f'author:"{query.author}"')
        if query.venue:
            terms.append(f'source:"{query.venue}"')
        params = {"q": " ".join(terms)}
        if query.year_from is not None:
            params["as_ylo"] = str(query.year_from)
        if query.year_to is not None:
            params["as_yhi"] = str(query.year_to)
        papers, total, next_start = await self._pages(params, limit=query.limit, operation="search")
        if query.open_access is not None:
            papers = [paper for paper in papers if paper.open_access == query.open_access]
        execution = {"text": "provider"}
        if query.author:
            execution["author"] = "provider"
        if query.venue:
            execution["venue"] = "provider"
        if query.year_from is not None:
            execution["year_from"] = "provider"
        if query.year_to is not None:
            execution["year_to"] = "provider"
        if query.open_access is not None:
            execution["open_access"] = "local"
        return ProviderBatch(
            papers=papers,
            total_available=total,
            truncated=next_start is not None or (total is not None and total > len(papers)),
            next_cursor=str(next_start) if next_start is not None else None,
            filter_execution=execution,
        )

    async def resolve(self, identifier: str) -> Paper | None:
        normalized = identifier.strip()
        if normalized.casefold().startswith("google_scholar:"):
            normalized = normalized.split(":", 1)[1]
        clusters = self._cluster_ids(normalized)
        queries = [{"cluster": c} for c in clusters] if clusters else [{"q": f'"{normalized}"'}]
        for params in queries:
            # Seed lookup needs a small candidate set; the separate discovery
            # phase still traverses all visible version pages within its budget.
            payload = await self._query({**params, "num": "3", "no_cache": "true"}, "resolve")
            items = payload.get("organic_results", [])
            if items:
                seed = self._paper(items[0])
                result = await self._discover(seed, clusters)
                # Explicit numeric inputs are also caller-supplied cites-list
                # hints (the public CLI has always accepted Cited by IDs).
                # Empty All Versions is not evidence that such a list is empty.
                # Retain the hint, but do NOT mint a verified identity claim.
                known = {str(r.retrieval_context.get("cites_id")) for r in result.source_records}
                known.update(
                    str(r.retrieval_context.get(key))
                    for r in result.source_records
                    for key in ("cluster_id", "lookup_cluster_id")
                    if r.retrieval_context.get("cites_id")
                )
                for cites_id in clusters:
                    if cites_id not in known:
                        result.source_records.append(
                            SourceRecord(
                                provider=self.name,
                                source_record_id=f"cites_list:{cites_id}",
                                source_url=f"https://scholar.google.com/scholar?cites={cites_id}",
                                retrieval_context={
                                    "cites_id": cites_id,
                                    "seed_alias_evidence": "caller_supplied_not_verified",
                                },
                            )
                        )
                return result
        return None

    async def references(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        return ProviderBatch()

    async def citations(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        normalized = identifier.strip()
        if normalized.casefold().startswith("google_scholar:"):
            normalized = normalized.split(":", 1)[1]
        cites_ids = self._cluster_ids(normalized)
        if not cites_ids:
            seed = await self.resolve(identifier)
            if seed is None:
                return ProviderBatch()
            traversal = self.traversal_identifier(seed, "")
            cites_ids = self._cluster_ids(traversal.removeprefix("google_scholar:"))
        papers: list[Paper] = []
        outcomes = []
        failures: list[Exception] = []
        batches = []
        for cites_id in cites_ids:
            try:
                batch = await self._citation_pages({"cites": cites_id}, limit=limit)
            except (httpx.HTTPError, ValueError) as exc:
                failures.append(exc)
                outcomes.append(
                    {"cites_id": cites_id, "status": "failed", "error_code": type(exc).__name__}
                )
            else:
                batches.append(batch)
                # Deliberately keep the same result in two different cites lists.
                # Only ScholarService performs cross-list/cross-provider identity merging.
                papers.extend(batch.papers)
                outcomes.append(
                    {
                        "cites_id": cites_id,
                        "status": "partial" if batch.truncated else "complete",
                        "retrieved_count": len(batch.papers),
                        "total_available": batch.total_available,
                        **batch.context,
                    }
                )
        if not batches and failures:
            raise failures[0]
        incomplete = bool(failures) or any(b.truncated for b in batches)
        return ProviderBatch(
            papers=papers,
            # Counts from different cites IDs overlap and are not additive.
            total_available=batches[0].total_available if len(cites_ids) == 1 and batches else None,
            truncated=incomplete,
            status=RunStatus.PARTIAL if incomplete else None,
            next_cursor=batches[0].next_cursor if len(batches) == 1 else None,
            context={
                "cluster_outcomes": outcomes,
                "limit_scope": "per_cites_id",
                "cross_cites_deduplication": "service",
            },
        )

    async def _pages(
        self, params: dict[str, str], *, limit: int, operation: str
    ) -> tuple[list[Paper], int | None, int | None]:
        papers: list[Paper] = []
        start = 0
        total: int | None = None
        next_start: int | None = None
        while len(papers) < limit:
            page_size = min(20, limit - len(papers))
            payload = await self._query(
                {**params, "start": str(start), "num": str(page_size)}, operation
            )
            items = payload.get("organic_results", [])
            papers.extend(self._paper(item) for item in items)
            total = payload.get("search_information", {}).get("total_results", total)
            next_link = payload.get("serpapi_pagination", {}).get("next")
            next_start = start + len(items) if next_link and items else None
            if next_start is None or not items:
                break
            start = next_start
        return papers[:limit], total, next_start

    async def _citation_pages(self, params: dict[str, str], *, limit: int) -> ProviderBatch:
        """Fresh, next-link-driven rounds; stability is observed, not guaranteed.

        Repeated observations within one cites list are coalesced by result ID.
        Cross-cites observations are retained by citations() for service merging.
        Totals are diagnostic only: never infer page offsets from a total.
        """
        if limit < 1:
            return ProviderBatch()
        observed: dict[str, Paper] = {}
        previous: set[str] | None = None
        total: int | None = None
        rounds = []
        stable = False
        reason = "round_budget"
        next_cursor = None
        max_pages = (limit + 9) // 10 + 2
        for round_index in range(1, self._citation_rounds + 1):
            current = {
                **params,
                "start": "0",
                "num": str(min(10, limit)),
                "filter": "0",
                "no_cache": "true",
            }
            round_ids: set[str] = set()
            pages = []
            finished = False
            error = None
            for _ in range(max_pages):
                try:
                    payload = await self._query(current, "citations")
                except (httpx.HTTPError, ValueError) as exc:
                    if not observed:
                        raise
                    error = type(exc).__name__
                    reason = "request_failed"
                    next_cursor = current["start"]
                    break
                reported = payload.get("search_information", {}).get("total_results")
                if isinstance(reported, int) and reported >= 0:
                    total = max(total or 0, reported)
                items = payload.get("organic_results", [])
                pages.append(
                    {
                        "start": current["start"],
                        "returned": len(items),
                        "reported_total": reported,
                        # Compare archived HTML with this exact JSON response,
                        # not with another fresh search that may have changed.
                        "search_id": (payload.get("search_metadata") or {}).get("id"),
                    }
                )
                for item in items:
                    paper = self._paper(item)
                    round_ids.add(paper.record_id)
                    if paper.record_id not in observed and len(observed) < limit:
                        record = paper.source_records[0]
                        record.retrieval_context["seed_cites_id"] = params["cites"]
                        record.retrieval_context["observations"] = []
                        observed[paper.record_id] = paper
                    if paper.record_id in observed:
                        observed[paper.record_id].source_records[0].retrieval_context[
                            "observations"
                        ].append({"round": round_index, "start": current["start"]})
                try:
                    following = self._next_params(payload, current)
                except ValueError:
                    error = "invalid_pagination"
                    reason = error
                    break
                if following is None:
                    finished = True
                    next_cursor = None
                    break
                next_cursor = following["start"]
                if len(observed) >= limit:
                    reason = "result_limit"
                    break
                current = following
            rounds.append(
                {
                    "round": round_index,
                    "pages": pages,
                    "observed_count": len(round_ids),
                    "ended": finished,
                    "error": error,
                }
            )
            if error or reason == "result_limit":
                break
            # Two complete rounds must return the SAME IDs, not just the same
            # count. An advertised coverage gap prevents a false 'complete'.
            stable = finished and previous == round_ids
            if stable and (total is None or total <= len(observed)):
                reason = "stable"
                break
            previous = round_ids if finished else None
            if len(observed) >= limit:
                reason = "result_limit"
                break
        incomplete = reason != "stable"
        return ProviderBatch(
            papers=list(observed.values()),
            total_available=total,
            truncated=incomplete,
            status=RunStatus.PARTIAL if incomplete else None,
            next_cursor=next_cursor,
            context={
                "rounds": rounds,
                "stop_reason": reason,
                "stable": stable,
                "no_cache": True,
                "max_rounds": self._citation_rounds,
            },
        )

    def _paper(self, item: dict[str, Any]) -> Paper:
        inline = item.get("inline_links") or {}
        cited_by = inline.get("cited_by") or {}
        versions = inline.get("versions") or {}
        cites_id = str(cited_by.get("cites_id") or "")
        cluster_id = str(versions.get("cluster_id") or "")
        result_id = str(item.get("result_id") or cites_id)
        if not result_id:
            material = f"{item.get('title', '')}\0{item.get('link', '')}".encode()
            result_id = hashlib.sha256(material).hexdigest()[:24]
        provenance = Provenance(provider=self.name, source_record_id=result_id)
        summary = str((item.get("publication_info") or {}).get("summary") or "")
        year_matches = YEAR_PATTERN.findall(summary)
        authors_data = (item.get("publication_info") or {}).get("authors") or []
        authors = [
            Author(name=str(author.get("name"))) for author in authors_data if author.get("name")
        ]
        if not authors and summary:
            author_text = summary.split(" - ", 1)[0]
            authors = [
                Author(name=value.strip()) for value in author_text.split(",") if value.strip()
            ]
        searchable = " ".join(str(item.get(field) or "") for field in ("title", "link", "snippet"))
        searchable += " " + summary.replace("arxiv. org", "arxiv.org")
        doi_match = DOI_PATTERN.search(searchable)
        arxiv_match = ARXIV_URL_PATTERN.search(searchable)
        identifiers = []
        if cites_id:
            identifiers.append(
                IdentifierClaim(
                    scheme=IdentifierScheme.GOOGLE_SCHOLAR,
                    value=cites_id,
                    authority="Google Scholar cites list",
                    provenance=provenance,
                )
            )
        if doi_match:
            identifiers.append(
                IdentifierClaim(
                    scheme=IdentifierScheme.DOI,
                    value=normalize_doi(doi_match.group(0)),
                    authority="DOI",
                    provenance=provenance,
                )
            )
        if arxiv_match:
            arxiv_id = normalize_arxiv_id(arxiv_match.group(1), keep_version=False)
            if arxiv_id:
                identifiers.append(
                    IdentifierClaim(
                        scheme=IdentifierScheme.ARXIV,
                        value=arxiv_id,
                        authority="arXiv",
                        provenance=provenance,
                    )
                )
        resources = item.get("resources") or []
        pdf_url = next(
            (
                resource.get("link")
                for resource in resources
                if str(resource.get("file_format", "")).casefold() == "pdf"
            ),
            None,
        )
        count = cited_by.get("total")
        return Paper(
            record_id=f"google_scholar:{result_id}",
            work_family_id=f"google_scholar_cluster:{cluster_id}" if cluster_id else None,
            title=str(item.get("title") or "Untitled Google Scholar result"),
            abstract=item.get("snippet"),
            authors=authors,
            publication_year=int(year_matches[-1]) if year_matches else None,
            landing_page_url=item.get("link"),
            pdf_url=pdf_url,
            open_access=pdf_url is not None,
            identifiers=identifiers,
            citation_counts=(
                [
                    CitationCountClaim(
                        provider=self.name,
                        count=int(count),
                        source_record_id=result_id,
                    )
                ]
                if isinstance(count, int) and count >= 0
                else []
            ),
            source_records=[
                SourceRecord(
                    provider=self.name,
                    source_record_id=result_id,
                    source_url=item.get("link"),
                    provider_rank=item.get("position"),
                    retrieval_context={
                        "cluster_id": cluster_id or None,
                        "cites_id": cites_id or None,
                    },
                )
            ],
            field_provenance={
                field: [provenance]
                for field in [
                    "title",
                    "abstract",
                    "authors",
                    "publication_year",
                    "open_access",
                ]
            },
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @staticmethod
    def _cluster_ids(value: str) -> list[str]:
        values = [item.strip() for item in value.split(",")]
        if not values or any(not item.isdigit() for item in values):
            return []
        return list(dict.fromkeys(values))
