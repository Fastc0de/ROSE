"""Shared fakes: no test touches the network or needs an API key."""

from __future__ import annotations

from dataclasses import replace
from typing import Callable

import httpx
import pytest

from ros.config import Settings
from ros.connectors.base import SearchHit
from ros.connectors.search import StaticSearchBackend
from ros.db import Database
from ros.llm import FakeLLM
from ros.registry import build_app
from ros.security import SafeFetcher

PUBLIC_IP = "93.184.216.34"


class FakeWeb:
    """In-memory websites keyed by URL. Routes by the Host header, so DNS pinning is exercised."""

    def __init__(self) -> None:
        self.pages: dict[str, tuple[int, dict[str, str], bytes]] = {}
        self.requests: list[httpx.Request] = []

    def add(self, url: str, body: str | bytes = "", *, status: int = 200, content_type: str = "text/html",
            headers: dict[str, str] | None = None) -> None:
        data = body.encode() if isinstance(body, str) else body
        self.pages[url] = (status, {"content-type": content_type, **(headers or {})}, data)

    def html(self, url: str, title: str, paragraphs: list[str]) -> None:
        body = "".join(f"<p>{p}</p>" for p in paragraphs)
        self.add(url, f"<html><head><title>{title}</title></head><body><article>{body}</article></body></html>")

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = f"{request.url.scheme}://{request.headers['host']}{request.url.raw_path.decode()}"
        if url not in self.pages:
            return httpx.Response(404)
        status, headers, body = self.pages[url]
        return httpx.Response(status, headers=headers, content=body)

    def hits(self, url: str) -> int:
        return sum(1 for r in self.requests
                   if f"{r.url.scheme}://{r.headers['host']}{r.url.raw_path.decode()}" == url)


@pytest.fixture
def web() -> FakeWeb:
    return FakeWeb()


@pytest.fixture
def resolver() -> Callable[[str], list[str]]:
    table = {"internal.example": ["10.0.0.5"], "rebind.example": ["127.0.0.1"]}
    return lambda host: table.get(host, [PUBLIC_IP])


@pytest.fixture
def fetcher(web: FakeWeb, resolver) -> SafeFetcher:
    return SafeFetcher(httpx.Client(transport=httpx.MockTransport(web.handler)), resolver=resolver, pin_dns=True)


@pytest.fixture
def db() -> Database:
    database = Database(":memory:")
    yield database
    database.close()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(db_path=str(tmp_path / "ros.db"), reports_dir=str(tmp_path / "reports"), llm="fake")


def static_search(results: dict[str, list[SearchHit]]) -> StaticSearchBackend:
    """Search backend returning `results[q]` for the first key contained in the query (else nothing)."""
    def fn(query: str, limit: int) -> list[SearchHit]:
        for key, hits in results.items():
            if key in query.lower():
                return hits
        return []
    return StaticSearchBackend(fn)


@pytest.fixture
def make_app(settings, fetcher, db):
    def factory(handler, search, **settings_changes):
        s = replace(settings, **settings_changes) if settings_changes else settings
        return build_app(s, llm=FakeLLM(handler), search=search, fetcher=fetcher, db=db)
    return factory
