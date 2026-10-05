"""Composition root: builds the database, model, fetcher, search and connectors once."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .config import Settings
from .connectors.base import SearchBackend
from .connectors.feeds import FeedConnector, RedditConnector, WebPageConnector, YouTubeConnector
from .connectors.native import (FacebookConnector, InstagramConnector, RedditAPIConnector, XAPIConnector,
                                YouTubeAPIConnector)
from .connectors.search import BraveBackend, DuckDuckGoBackend, SearchWatchConnector, SearxngBackend
from .db import Database
from .errors import ErrorKind, RosError
from .llm import LLM, AnthropicLLM, FakeLLM
from .security import SafeFetcher


@dataclass
class App:
    settings: Settings
    db: Database
    _llm: LLM | None              # when injected (tests, offline), one model serves every role
    fetcher: SafeFetcher
    search: SearchBackend
    connectors: dict
    _llms: dict = field(default_factory=dict)

    def llm(self, role: str) -> LLM:
        """Model for a role (orchestrator | validator | worker). Built lazily, so read-only
        commands work without model credentials."""
        if self._llm is not None:
            return self._llm
        if role not in self._llms:
            self._llms[role] = build_llm(self.settings, role, shared=next(iter(self._llms.values()), None))
        return self._llms[role]

    def connector(self, kind: str):
        if kind not in self.connectors:
            raise RosError(ErrorKind.UNSUPPORTED_FORMAT,
                           f"tipo de fuente no soportado: {kind!r}. Tipos: {', '.join(sorted(self.connectors))}.")
        return self.connectors[kind]


def build_search(settings: Settings) -> SearchBackend:
    if settings.search_backend == "brave":
        return BraveBackend(os.environ.get(settings.brave_api_key_env, ""))
    if settings.search_backend == "searxng":
        return SearxngBackend(settings.searxng_url)
    if settings.search_backend == "duckduckgo":
        return DuckDuckGoBackend()
    raise ValueError(f"unknown search backend {settings.search_backend!r}")


def build_llm(settings: Settings, role: str, shared: LLM | None = None) -> LLM:
    model, effort = settings.role(role)
    if settings.llm == "anthropic":
        client = shared.client if isinstance(shared, AnthropicLLM) else None
        return AnthropicLLM(model=model, effort=effort, workspace_id=settings.anthropic_workspace_id, client=client)
    if settings.llm == "fake":
        from .offline import offline_handler
        return FakeLLM(offline_handler)
    raise ValueError(f"unknown llm {settings.llm!r}")


def build_connectors(fetcher: SafeFetcher, search: SearchBackend, db: Database | None = None,
                     settings: Settings | None = None, env: dict | None = None) -> dict:
    connectors = {
        "rss": FeedConnector(fetcher),
        "youtube": YouTubeConnector(fetcher),
        "reddit": RedditConnector(fetcher),
        "web": WebPageConnector(fetcher),
        "search": SearchWatchConnector(search, fetcher),
    }
    if db is not None:
        s = settings or Settings()
        connectors.update({
            "youtube_api": YouTubeAPIConnector(db, key_env=s.youtube_api_key_env, daily_quota=s.youtube_daily_quota,
                                               env=env),
            "reddit_api": RedditAPIConnector(db, id_env=s.reddit_client_id_env, secret_env=s.reddit_client_secret_env,
                                             env=env),
            "x": XAPIConnector(db, token_env=s.x_bearer_token_env, env=env),
            "instagram": InstagramConnector(db, token_env=s.meta_access_token_env, ig_user_id=s.instagram_user_id,
                                            env=env),
            "facebook": FacebookConnector(db, token_env=s.meta_access_token_env, env=env),
        })
    return connectors


def build_app(settings: Settings, *, llm: LLM | None = None, search: SearchBackend | None = None,
              fetcher: SafeFetcher | None = None, db: Database | None = None) -> App:
    db = db or Database(settings.db_path)
    fetcher = fetcher or SafeFetcher(max_bytes=settings.max_fetch_bytes)
    search = search or build_search(settings)
    return App(settings, db, llm, fetcher, search, build_connectors(fetcher, search, db, settings))
