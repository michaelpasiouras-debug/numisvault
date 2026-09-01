"""Provider abstraction for CoinBids Price Research.

This module deliberately separates *where listings come from* from the
numismatic candidate funnel (identity validation, FX, shipping and sorting).
A provider must return raw listing candidates plus structured source status.

It does NOT attempt to bypass CAPTCHAs, WAFs or human-verification systems.
Blocked sources report an explicit availability error so another authorized
provider/feed can be plugged in without changing the matching engine.
"""
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence


PROVIDER_OK = "ok"
PROVIDER_UNAVAILABLE = "unavailable"
PROVIDER_ERROR = "error"
PROVIDER_NOT_CONFIGURED = "not_configured"


@dataclass
class ProviderResult:
    provider: str
    query: str
    offers: List[Dict[str, Any]] = field(default_factory=list)
    used_url: Optional[str] = None
    error: Optional[str] = None
    availability: str = PROVIDER_OK


class PriceResearchProvider:
    name = "provider"

    def search(self, query: str, payload: Dict[str, Any]) -> ProviderResult:
        raise NotImplementedError


class CallableProvider(PriceResearchProvider):
    """Adapter for an existing fetch function.

    The callable contract is intentionally the same as the legacy CoinBids
    fetchers: ``fetch(query, payload) -> (offers, used_url, error)``. This lets
    us migrate incrementally without rewriting the proven candidate funnel.
    """

    def __init__(self, name: str, fetch: Callable):
        self.name = name
        self._fetch = fetch

    def search(self, query: str, payload: Dict[str, Any]) -> ProviderResult:
        try:
            offers, used_url, error = self._fetch(query, payload)
        except Exception as exc:
            return ProviderResult(
                provider=self.name,
                query=query,
                error=f"{type(exc).__name__}: {exc}",
                availability=PROVIDER_ERROR,
            )

        availability = PROVIDER_OK
        if error:
            low = str(error).lower()
            if any(token in low for token in (
                "human-verification", "human verification", "checkpoint",
                "captcha", "anti-bot", "blocked automated search",
                "temporarily unavailable", "results currently unavailable",
                "did not provide searchable listing data", "creoline",
            )):
                availability = PROVIDER_UNAVAILABLE
            elif not offers:
                availability = PROVIDER_ERROR

        return ProviderResult(
            provider=self.name,
            query=query,
            offers=list(offers or []),
            used_url=used_url,
            error=error,
            availability=availability,
        )


class ProviderRegistry:
    """Ordered registry of listing providers.

    Provider order is explicit. Results are not ranked here: CoinBids' existing
    hard filters, shipping/FX enrichment and global delivered-price sort remain
    the single source of truth after candidates are collected.
    """

    def __init__(self, providers: Optional[Iterable[PriceResearchProvider]] = None):
        self._providers: List[PriceResearchProvider] = list(providers or [])

    def register(self, provider: PriceResearchProvider) -> None:
        if any(p.name == provider.name for p in self._providers):
            raise ValueError(f"provider already registered: {provider.name}")
        self._providers.append(provider)

    @property
    def providers(self) -> List[PriceResearchProvider]:
        return list(self._providers)

    def search_all(self, query: str, payload: Dict[str, Any]) -> List[ProviderResult]:
        return [provider.search(query, payload) for provider in self._providers]


def flatten_provider_results(results: Iterable[ProviderResult]):
    """Convert provider results into the legacy funnel inputs.

    Returns ``(offers, used, errors, provider_status)`` so coin_search can keep
    its downstream filtering/ranking semantics unchanged during migration.
    """
    offers: List[Dict[str, Any]] = []
    used: List[Dict[str, str]] = []
    errors: List[Dict[str, str]] = []
    status: Dict[str, str] = {}

    for result in results:
        status[result.provider] = result.availability
        if result.used_url:
            used.append({"query": result.query, "source": result.provider, "url": result.used_url})
        if result.error:
            errors.append({"query": result.query, "source": result.provider, "error": result.error})
        for offer in result.offers:
            row = dict(offer)
            row.setdefault("dealer_source", result.provider)
            # Copy the nested list as well: a shallow row copy must not mutate
            # a provider-owned candidate when provenance is appended.
            row["_source_queries"] = list(row.get("_source_queries") or [])
            if result.query not in row["_source_queries"]:
                row["_source_queries"].append(result.query)
            offers.append(row)

    return offers, used, errors, status


def consolidate_provider_statuses(
    results: Iterable[ProviderResult],
    provider_names: Optional[Sequence[str]] = None,
) -> Dict[str, str]:
    """Aggregate query-level availability into one status per provider.

    A single successful request proves that the provider was reachable. A
    provider is ``unavailable`` only when every recorded request was blocked or
    otherwise explicitly unavailable. Mixed technical errors and unavailable
    responses remain ``error`` so a partial WAF signal cannot hide another
    failure mode.

    ``provider_names`` may include expected providers that were not registered
    or executed; those are reported as ``not_configured``.
    """
    grouped: Dict[str, List[str]] = {}
    for result in results:
        grouped.setdefault(result.provider, []).append(result.availability)

    ordered_names = list(provider_names or [])
    for name in grouped:
        if name not in ordered_names:
            ordered_names.append(name)

    statuses: Dict[str, str] = {}
    for name in ordered_names:
        values = grouped.get(name, [])
        if not values:
            statuses[name] = PROVIDER_NOT_CONFIGURED
        elif PROVIDER_OK in values:
            statuses[name] = PROVIDER_OK
        elif all(value == PROVIDER_UNAVAILABLE for value in values):
            statuses[name] = PROVIDER_UNAVAILABLE
        else:
            statuses[name] = PROVIDER_ERROR
    return statuses
