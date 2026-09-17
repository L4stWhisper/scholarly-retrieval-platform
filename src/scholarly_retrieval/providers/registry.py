"""Extensible provider registration without coupling the service to adapters."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass

from ..storage import SQLiteStore
from .acl_anthology import AclAnthologyProvider
from .ads import ADSProvider
from .arxiv import ArxivProvider
from .base import ScholarlyProvider
from .crossref import CrossrefProvider
from .datacite import DataCiteProvider
from .dblp import DblpProvider
from .europe_pmc import EuropePmcProvider
from .inspire import InspireProvider
from .openaire import OpenAireProvider
from .openalex import OpenAlexProvider
from .opencitations import OpenCitationsProvider
from .openreview import OpenReviewProvider
from .semantic_scholar import SemanticScholarProvider
from .serpapi_google_scholar import SerpApiGoogleScholarProvider

ProviderFactory = Callable[[SQLiteStore | None], ScholarlyProvider]
EnablePredicate = Callable[[], bool]


@dataclass(frozen=True)
class ProviderRegistration:
    name: str
    factory: ProviderFactory
    enabled: EnablePredicate


class ProviderRegistry:
    """Ordered provider factory registry used by every application adapter.

    A new connector only needs to implement ScholarlyProvider and register one
    factory. ScholarService remains unchanged, and optional licensed providers
    can be gated by an environment predicate without constructing them.
    """

    def __init__(self) -> None:
        self._registrations: dict[str, ProviderRegistration] = {}

    def register(
        self,
        name: str,
        factory: ProviderFactory,
        *,
        enabled: EnablePredicate | None = None,
    ) -> None:
        normalized = name.strip()
        if not normalized:
            raise ValueError("provider registration name must not be blank")
        if normalized in self._registrations:
            raise ValueError(f"provider already registered: {normalized}")
        self._registrations[normalized] = ProviderRegistration(
            name=normalized,
            factory=factory,
            enabled=enabled or (lambda: True),
        )

    def create_all(self, store: SQLiteStore | None = None) -> list[ScholarlyProvider]:
        providers: list[ScholarlyProvider] = []
        for registration in self._registrations.values():
            if not registration.enabled():
                continue
            provider = registration.factory(store)
            if provider.name != registration.name:
                raise ValueError(
                    "provider factory name mismatch: "
                    f"registered={registration.name}, actual={provider.name}"
                )
            providers.append(provider)
        return providers

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._registrations)


def default_provider_registry() -> ProviderRegistry:
    """Build a fresh registry so callers can safely extend it per service."""

    registry = ProviderRegistry()
    registry.register(
        "ads",
        lambda store: ADSProvider(store=store),
        enabled=lambda: bool(os.getenv("ADS_API_TOKEN")),
    )
    registry.register("openalex", lambda store: OpenAlexProvider(store=store))
    registry.register("openaire", lambda store: OpenAireProvider(store=store))
    registry.register("semantic_scholar", lambda store: SemanticScholarProvider(store=store))
    registry.register("crossref", lambda store: CrossrefProvider(store=store))
    registry.register("datacite", lambda store: DataCiteProvider(store=store))
    registry.register("dblp", lambda store: DblpProvider(store=store))
    registry.register("acl_anthology", lambda store: AclAnthologyProvider(store=store))
    registry.register("arxiv", lambda store: ArxivProvider(store=store))
    registry.register("openreview", lambda store: OpenReviewProvider(store=store))
    registry.register("europe_pmc", lambda store: EuropePmcProvider(store=store))
    registry.register("inspire", lambda store: InspireProvider(store=store))
    registry.register("opencitations", lambda store: OpenCitationsProvider(store=store))
    registry.register(
        "google_scholar_serpapi",
        lambda store: SerpApiGoogleScholarProvider(store=store),
        enabled=lambda: bool(os.getenv("SERPAPI_API_KEY")),
    )
    return registry
