"""Composition root: builds the database, model, fetcher, search and connectors once."""

from __future__ import annotations

import os
from dataclasses import dataclass

from .config import Settings
from .connectors.base import SearchBackend
from .connectors.feeds import FeedConnector, RedditConnector, WebPageConnector, YouTubeConnector
from .connectors.search import BraveBackend, DuckDuckGoBackend, SearchWatchConnector, SearxngBackend
from .db import Database
from .errors import ErrorKind, RosError
from .llm import LLM, AnthropicLLM, FakeLLM
from .security import SafeFetcher


@dataclass
class App:
    settings: Settings
    db: Database
    _llm: LLM | None
    fetcher: SafeFetcher
    search: SearchBackend
    connectors: dict

    @property
    def llm(self) -> LLM:
        # Built lazily so read-only commands work without model credentials.
        if self._llm is None:
            self._llm = build_llm(self.settings)
        return self._llm

    def connector(self, kind: str):
        if kind not in self.connectors:
            raise RosError(ErrorKind.UNSUPPORTED_FORMAT,
                           f"tipo de fuente no soportado: {kind!r}. Disponibles: {', '.join(sorted(self.connectors))}. "
                           "Instagram, Facebook y X no están disponibles: requieren APIs oficiales autorizadas.")
        return self.connectors[kind]


def build_search(settings: Settings) -> SearchBackend:
    if settings.search_backend == "brave":
        return BraveBackend(os.environ.get(settings.brave_api_key_env, ""))
    if settings.search_backend == "searxng":
        return SearxngBackend(settings.searxng_url)
    if settings.search_backend == "duckduckgo":
        return DuckDuckGoBackend()
    raise ValueError(f"unknown search backend {settings.search_backend!r}")


def build_llm(settings: Settings) -> LLM:
    if settings.llm == "anthropic":
        return AnthropicLLM(model=settings.model, effort=settings.effort)
    if settings.llm == "fake":
        from .offline import offline_handler
        return FakeLLM(offline_handler)
    raise ValueError(f"unknown llm {settings.llm!r}")


def build_connectors(fetcher: SafeFetcher, search: SearchBackend) -> dict:
    return {
        "rss": FeedConnector(fetcher),
        "youtube": YouTubeConnector(fetcher),
        "reddit": RedditConnector(fetcher),
        "web": WebPageConnector(fetcher),
        "search": SearchWatchConnector(search, fetcher),
    }


def build_app(settings: Settings, *, llm: LLM | None = None, search: SearchBackend | None = None,
              fetcher: SafeFetcher | None = None, db: Database | None = None) -> App:
    db = db or Database(settings.db_path)
    fetcher = fetcher or SafeFetcher(max_bytes=settings.max_fetch_bytes)
    search = search or build_search(settings)
    return App(settings, db, llm, fetcher, search, build_connectors(fetcher, search))
