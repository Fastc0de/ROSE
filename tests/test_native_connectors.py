"""Official-API connectors against recorded-shape fixtures (no network, no real credentials)."""

import httpx
import pytest

from ros.connectors.base import SourceSpec
from ros.connectors.native import (FacebookConnector, InstagramConnector, RedditAPIConnector, XAPIConnector,
                                   YouTubeAPIConnector)
from ros.errors import ErrorKind


def client(routes, calls):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        for (method, prefix), fn in routes.items():
            if request.method == method and str(request.url).startswith(prefix):
                return fn(request)
        return httpx.Response(404, json={"error": "not found"})
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_without_credentials_connectors_are_unconfigured_and_refuse_to_run(db):
    for c in (YouTubeAPIConnector(db, env={}), RedditAPIConnector(db, env={}), XAPIConnector(db, env={}),
              InstagramConnector(db, env={}), FacebookConnector(db, env={})):
        st = c.status()
        assert st.state == "unconfigured" and not st.usable
        res = c.sync(SourceSpec(c.connector_id, "x"), None)
        assert res.error and res.error.kind == ErrorKind.INVALID_CREDENTIALS and not res.items


def test_youtube_api_uploads_with_comments_and_quota(db):
    calls = []
    yt = "https://www.googleapis.com/youtube/v3"
    routes = {
        ("GET", f"{yt}/videoCategories"): lambda r: httpx.Response(200, json={"items": [{}]}),
        ("GET", f"{yt}/channels"): lambda r: httpx.Response(200, json={"items": [{
            "id": "UC" + "a" * 22, "snippet": {"title": "Canal"},
            "contentDetails": {"relatedPlaylists": {"uploads": "UU" + "a" * 22}}}]}),
        ("GET", f"{yt}/playlistItems"): lambda r: httpx.Response(200, json={"items": [
            {"contentDetails": {"videoId": "v1"}}, {"contentDetails": {"videoId": "v2"}}]}),
        ("GET", f"{yt}/videos"): lambda r: httpx.Response(200, json={"items": [
            {"id": "v1", "snippet": {"title": "Vídeo 1", "description": "desc 1", "publishedAt": "2026-10-01T00:00:00Z",
                                     "channelTitle": "Canal"}, "statistics": {"viewCount": "10", "commentCount": "2"}},
            {"id": "v2", "snippet": {"title": "Vídeo 2", "description": "desc 2"}, "statistics": {"commentCount": "0"}}]}),
        ("GET", f"{yt}/commentThreads"): lambda r: httpx.Response(200, json={"items": [
            {"snippet": {"topLevelComment": {"snippet": {"authorDisplayName": "Ana", "likeCount": 3,
                                                         "textDisplay": "Muy buen análisis"}}}}]}),
    }
    c = YouTubeAPIConnector(db, env={"YOUTUBE_API_KEY": "k"}, daily_quota=50, client=client(routes, calls))
    assert c.status().state == "degraded"                      # credentials but no recent probe
    assert c.probe().state == "ready" and c.status().state == "ready"
    spec = c.validate("@canal")
    assert spec.locator == "UC" + "a" * 22 and spec.label == "Canal"
    res = c.sync(spec, None)
    assert [i.external_id for i in res.items] == ["yt:video:v1", "yt:video:v2"]
    assert "Muy buen análisis" in res.items[0].text and res.items[0].meta["views"] == 10
    again = c.sync(spec, res.cursor)
    assert "Comentarios" not in again.items[0].text             # comments fetched once per video
    used = db.one("SELECT units FROM quota_usage WHERE connector='youtube_api'")["units"]
    assert used == 1 + 1 + 1 + 1 + 1 + 1 + 1 + 1               # probe, channel, channel, list, videos, comments, list, videos
    assert all("key=k" in str(r.url) for r in calls)


def test_youtube_quota_is_enforced_before_calling(db):
    calls = []
    c = YouTubeAPIConnector(db, env={"YOUTUBE_API_KEY": "k"}, daily_quota=50, client=client({}, calls))
    db.execute("INSERT OR REPLACE INTO connector_probes VALUES ('youtube_api','ready','ok',datetime('now'))")
    db.execute("INSERT INTO quota_usage VALUES ('youtube_api', date('now'), 50)")
    res = c.sync(SourceSpec("youtube_api", "search:sodio"), None)
    assert res.error.kind == ErrorKind.RATE_LIMITED and not calls


def test_reddit_oauth_posts_and_comments(db):
    calls = []
    routes = {
        ("POST", "https://www.reddit.com/api/v1/access_token"): lambda r: httpx.Response(
            200, json={"access_token": "tok", "expires_in": 3600}),
        ("GET", "https://oauth.reddit.com/r/batteries/new"): lambda r: httpx.Response(200, json={"data": {"children": [
            {"kind": "t3", "data": {"name": "t3_a", "id": "a", "title": "Sodio", "selftext": "texto",
                                    "permalink": "/r/batteries/comments/a/x/", "created_utc": 1790000000,
                                    "num_comments": 1, "score": 5, "author": "u1", "subreddit": "batteries"}}]}}),
        ("GET", "https://oauth.reddit.com/comments/a"): lambda r: httpx.Response(200, json=[{}, {"data": {"children": [
            {"kind": "t1", "data": {"author": "u2", "score": 7, "body": "Comentario útil"}}]}}]),
    }
    c = RedditAPIConnector(db, env={"REDDIT_CLIENT_ID": "id", "REDDIT_CLIENT_SECRET": "s"}, client=client(routes, calls))
    res = c.sync(SourceSpec("reddit_api", "r/batteries"), None)
    assert res.error is None and res.items[0].external_id == "t3_a"
    assert "Comentario útil" in res.items[0].text and res.items[0].url.startswith("https://www.reddit.com/r/")
    assert calls[-1].headers["authorization"] == "Bearer tok"
    assert res.cursor["seen"] == ["t3_a"]


def test_x_uses_since_id_cursor_and_maps_errors(db):
    calls = []
    routes = {("GET", "https://api.x.com/2/tweets/search/recent"): lambda r: httpx.Response(200, json={
        "data": [{"id": "9", "text": "Batería de sodio", "created_at": "2026-10-01T00:00:00Z"}],
        "meta": {"newest_id": "9"}})}
    c = XAPIConnector(db, env={"X_BEARER_TOKEN": "t"}, client=client(routes, calls))
    db.execute("INSERT OR REPLACE INTO connector_probes VALUES ('x','ready','ok',datetime('now'))")
    res = c.sync(SourceSpec("x", "search:sodio"), None)
    assert res.items[0].external_id == "9" and res.cursor["since_id"] == "9"
    c.sync(SourceSpec("x", "search:sodio"), res.cursor)
    assert "since_id=9" in str(calls[-1].url)

    denied = XAPIConnector(db, env={"X_BEARER_TOKEN": "t"},
                           client=client({("GET", "https://api.x.com"): lambda r: httpx.Response(401)}, []))
    assert denied.probe().state == "auth_expired"


def test_instagram_business_discovery_only_with_professional_account(db):
    calls = []
    routes = {("GET", "https://graph.facebook.com/v21.0/123"): lambda r: httpx.Response(200, json={
        "business_discovery": {"media": {"data": [{"id": "m1", "caption": "Nuevo producto", "permalink": "https://www.instagram.com/p/m1/",
                                                   "timestamp": "2026-10-01T00:00:00+0000", "media_type": "IMAGE"}]}}})}
    assert InstagramConnector(db, env={"META_ACCESS_TOKEN": "t"}).status().state == "unconfigured"   # no ig user id
    c = InstagramConnector(db, env={"META_ACCESS_TOKEN": "t"}, ig_user_id="123", client=client(routes, calls))
    db.execute("INSERT OR REPLACE INTO connector_probes VALUES ('instagram','ready','ok',datetime('now'))")
    res = c.sync(SourceSpec("instagram", "marca"), None)
    assert res.items[0].text == "Nuevo producto"
    assert calls[0].url.params["fields"].startswith("business_discovery.username(marca)")


@pytest.mark.parametrize("status,kind", [(429, ErrorKind.RATE_LIMITED), (403, ErrorKind.PLATFORM_BLOCKED),
                                         (500, ErrorKind.TRANSIENT), (404, ErrorKind.CONTENT_DELETED)])
def test_api_errors_are_typed(db, status, kind):
    c = FacebookConnector(db, env={"META_ACCESS_TOKEN": "t"},
                          client=client({("GET", "https://graph.facebook.com"): lambda r: httpx.Response(status)}, []))
    db.execute("INSERT OR REPLACE INTO connector_probes VALUES ('facebook','ready','ok',datetime('now'))")
    res = c.sync(SourceSpec("facebook", "123"), None)
    assert res.error.kind == kind
