"""Contracts implemented by all scholarly data providers."""

from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel, ConfigDict

from ..models import Paper, ProviderBatch, RelatedQuery, SearchQuery


class ProviderOperationError(Exception):
    """Expected provider limitation that should fail only its own branch."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ProviderCapabilities(BaseModel):
    """Executable capability and access manifest for one provider adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    keyword_search: bool = False
    advanced_search: bool = False
    resolve_id: bool = False
    references: str = "none"
    citations: str = "none"
    related: bool = False
    fulltext: bool = False
    search_filter_execution: dict[str, str] = {}
    pagination: dict[str, str] = {}
    access_tier: str = "public"
    credential_variables: list[str] = []
    terms_url: str | None = None
    redistribution_policy: str = "provider_terms_apply"


class ScholarlyProvider(ABC):
    name: str
    capabilities: ProviderCapabilities

    def plan_search_filter_execution(self, query: SearchQuery) -> dict[str, str]:
        """Return query-specific search execution capabilities.

        Most adapters have static capabilities. Providers with value-dependent
        filters (for example, a finite upstream work-type vocabulary) can
        override this hook without falsely advertising universal support.
        """

        return dict(self.capabilities.search_filter_execution)

    @abstractmethod
    async def search(self, query: SearchQuery) -> ProviderBatch:
        raise NotImplementedError

    @abstractmethod
    async def resolve(self, identifier: str) -> Paper | None:
        raise NotImplementedError

    @abstractmethod
    async def references(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        raise NotImplementedError

    @abstractmethod
    async def citations(self, identifier: str, *, limit: int = 100) -> ProviderBatch:
        raise NotImplementedError

    async def related(self, query: RelatedQuery) -> ProviderBatch:
        """Return related candidates when the provider advertises this capability."""

        return ProviderBatch(filter_execution={"related": "unsupported"})

    async def close(self) -> None:
        return None
