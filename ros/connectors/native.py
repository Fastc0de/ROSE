"""Native connectors over official APIs: YouTube Data API v3, Reddit OAuth, X API v2, Instagram
Graph API (business discovery) and Facebook Graph API (Pages).

A native connector is only usable when its credentials are configured; without them its status is
`unconfigured` and it can never be selected or shown as available. There is no scraping fallback:
public feeds stay separate connectors (`youtube`, `reddit`) with access_mode="feed".

The API hosts are fixed and trusted, so these use a plain HTTP client (like the search backends);
content they return is still untrusted data and is wrapped before reaching a model.
"""

from __future__ import annotations

import base64
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from ..db import Database, now_iso
from ..errors import ErrorKind, RosError
from .base import Capabilities, ConnectorStatus, NormalizedItem, SourceSpec, SyncResult

GRAPH = "https://graph.facebook.com/v21.0"
PROBE_TTL = timedelta(hours=24)
SEEN_KEEP = 1000
USER_AGENT = "python:ros-research:0.2 (by /u/ros-local)"


class _APIConnector:
    connector_id = "api"
    access_mode = "official_api"
    api_name = ""
    daily_units = 0           # 0 = no local quota accounting

    def __init__(self, db: Database, client: httpx.Client | None = None, env: dict[str, str] | None = None):
        self.db = db
        self.client = client or httpx.Client(timeout=20.0, headers={"User-Agent": USER_AGENT})
        self.env = env if env is not None else os.environ

    # -- contract -----------------------------------------------------------
    def missing_credentials(self) -> list[str]:
        return []

    def status(self) -> ConnectorStatus:
        missing = self.missing_credentials()
        if missing:
            return ConnectorStatus("unconfigured", f"faltan credenciales: {', '.join(missing)}")
        row = self.db.one("SELECT * FROM connector_probes WHERE connector=?", (self.connector_id,))
        if row and _aware(row["probed_at"]) > datetime.now(timezone.utc) - PROBE_TTL:
            return ConnectorStatus(row["state"], row["detail"] or "", row["probed_at"])
        return ConnectorStatus("degraded", "credenciales presentes; sin sonda reciente (ros connectors --probe)")

    def probe(self) -> ConnectorStatus:
        missing = self.missing_credentials()
        if missing:
            return ConnectorStatus("unconfigured", f"faltan credenciales: {', '.join(missing)}")
        try:
            detail = self._probe()
            state = "ready"
        except RosError as exc:
            state, detail = _state_for(exc), str(exc)
        ts = now_iso()
        self.db.execute("INSERT OR REPLACE INTO connector_probes (connector, state, detail, probed_at) VALUES (?,?,?,?)",
                        (self.connector_id, state, detail[:500], ts))
        return ConnectorStatus(state, detail, ts)

    def _probe(self) -> str:
        raise NotImplementedError

    def _require(self) -> None:
        st = self.status()
        if st.state == "degraded" and "sin sonda" in st.detail:
            st = self.probe()
        if not st.usable:
            kind = ErrorKind.INVALID_CREDENTIALS if st.state in ("unconfigured", "auth_expired") else \
                ErrorKind.RATE_LIMITED if st.state == "quota_blocked" else ErrorKind.PLATFORM_BLOCKED
            raise RosError(kind, f"{self.api_name}: {st.state} — {st.detail}")

    # -- HTTP ---------------------------------------------------------------
    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        try:
            resp = self.client.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            raise RosError(ErrorKind.TRANSIENT, f"{self.api_name}: timeout") from exc
        except httpx.TransportError as exc:
            raise RosError(ErrorKind.SOURCE_UNREACHABLE, f"{self.api_name}: {exc}") from exc
        code = resp.status_code
        if code < 400:
            try:
                return resp.json()
            except ValueError as exc:
                raise RosError(ErrorKind.UNSUPPORTED_FORMAT, f"{self.api_name}: respuesta no JSON") from exc
        body = resp.text[:300]
        if code == 401:
            raise RosError(ErrorKind.INVALID_CREDENTIALS, f"{self.api_name}: credenciales rechazadas (401)")
        if code == 429 or "quota" in body.lower() or "rate limit" in body.lower():
            retry = resp.headers.get("retry-after")
            raise RosError(ErrorKind.RATE_LIMITED, f"{self.api_name}: límite de uso ({code})",
                           retry_after=float(retry) if retry and retry.isdigit() else None)
        if code == 403:
            raise RosError(ErrorKind.PLATFORM_BLOCKED, f"{self.api_name}: permiso denegado (403): {body}")
        if code in (404, 410):
            raise RosError(ErrorKind.CONTENT_DELETED, f"{self.api_name}: recurso no encontrado ({code})")
        if code >= 500:
            raise RosError(ErrorKind.TRANSIENT, f"{self.api_name}: HTTP {code}")
        raise RosError(ErrorKind.SOURCE_UNREACHABLE, f"{self.api_name}: HTTP {code}: {body}")

    def _spend(self, units: int) -> None:
        """Reserve local quota before a call; refuse once the daily allowance is used up."""
        if not self.daily_units:
            return
        day = datetime.now(timezone.utc).date().isoformat()
        with self.db.tx():
            row = self.db.one("SELECT units FROM quota_usage WHERE connector=? AND day=?", (self.connector_id, day))
            used = row["units"] if row else 0
            if used + units > self.daily_units:
                raise RosError(ErrorKind.RATE_LIMITED, f"{self.api_name}: cuota diaria agotada ({used}/{self.daily_units})")
            self.db.execute("INSERT INTO quota_usage (connector, day, units) VALUES (?,?,?) ON CONFLICT(connector, day) "
                            "DO UPDATE SET units = units + excluded.units", (self.connector_id, day, units))

    @staticmethod
    def _seen(cursor: dict) -> list[str]:
        return list(cursor.get("seen", []))


def _aware(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _state_for(exc: RosError) -> str:
    return {ErrorKind.INVALID_CREDENTIALS: "auth_expired", ErrorKind.RATE_LIMITED: "quota_blocked",
            ErrorKind.PLATFORM_BLOCKED: "permission_blocked"}.get(exc.kind, "degraded")


# ============================================================================ YouTube
class YouTubeAPIConnector(_APIConnector):
    """Channel uploads (with metadata and top comments) and video search via YouTube Data API v3."""

    connector_id = "youtube_api"
    api_name = "YouTube Data API v3"
    base = "https://www.googleapis.com/youtube/v3"
    comments_per_video = 10
    videos_with_comments = 5

    def __init__(self, db: Database, key_env: str = "YOUTUBE_API_KEY", daily_quota: int = 10_000, **kw: Any):
        super().__init__(db, **kw)
        self.key_env = key_env
        self.daily_units = daily_quota

    def missing_credentials(self) -> list[str]:
        return [] if self.env.get(self.key_env) else [self.key_env]

    def capabilities(self) -> Capabilities:
        return Capabilities(self.connector_id, self.access_mode, ("validate", "sync", "search"), cursor=True,
                            notes="Subidas del canal con metadatos y comentarios destacados, o búsqueda "
                                  "(search:texto, 100 unidades). Cuota diaria contabilizada localmente.")

    def _get(self, path: str, units: int, **params: Any) -> dict:
        self._spend(units)
        return self._request("GET", f"{self.base}/{path}", params={**params, "key": self.env.get(self.key_env, "")})

    def _probe(self) -> str:
        data = self._get("videoCategories", 1, part="snippet", regionCode="US")
        return f"ok ({len(data.get('items', []))} categorías)"

    def validate(self, locator: str) -> SourceSpec:
        self._require()
        loc = locator.strip()
        if loc.lower().startswith("search:"):
            return SourceSpec(self.connector_id, loc, f"YouTube: {loc[7:].strip()}")
        m = re.search(r"(UC[\w-]{22})", loc)
        params = {"id": m.group(1)} if m else {"forHandle": "@" + re.sub(r"^.*?@", "", loc).strip("/")}
        data = self._get("channels", 1, part="snippet,contentDetails", **params)
        if not data.get("items"):
            raise RosError(ErrorKind.CONTENT_DELETED, f"canal de YouTube no encontrado: {locator!r}")
        ch = data["items"][0]
        return SourceSpec(self.connector_id, ch["id"], ch["snippet"]["title"])

    def sync(self, spec: SourceSpec, cursor: dict | None) -> SyncResult:
        cursor = dict(cursor or {})
        try:
            self._require()
            if spec.locator.lower().startswith("search:"):
                data = self._get("search", 100, part="snippet", q=spec.locator[7:].strip(), type="video",
                                 order="date", maxResults=25)
                ids = [i["id"]["videoId"] for i in data.get("items", []) if i.get("id", {}).get("videoId")]
            else:
                uploads = cursor.get("uploads")
                if not uploads:
                    ch = self._get("channels", 1, part="contentDetails", id=spec.locator)
                    if not ch.get("items"):
                        raise RosError(ErrorKind.CONTENT_DELETED, f"canal no encontrado: {spec.locator}")
                    uploads = ch["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
                    cursor["uploads"] = uploads
                data = self._get("playlistItems", 1, part="contentDetails", playlistId=uploads, maxResults=25)
                ids = [i["contentDetails"]["videoId"] for i in data.get("items", [])]
            if not ids:
                return SyncResult([], cursor)
            videos = self._get("videos", 1, part="snippet,statistics", id=",".join(ids[:50])).get("items", [])
            seen = self._seen(cursor)
            items, warnings, commented = [], [], 0
            for v in videos:
                sn, stats = v["snippet"], v.get("statistics", {})
                text = sn.get("description", "")
                if v["id"] not in seen and commented < self.videos_with_comments and stats.get("commentCount", "0") != "0":
                    commented += 1
                    try:
                        text += self._comments(v["id"])
                    except RosError as exc:
                        warnings.append(f"comentarios de {v['id']}: {exc}")
                items.append(NormalizedItem(
                    external_id=f"yt:video:{v['id']}", url=f"https://www.youtube.com/watch?v={v['id']}",
                    title=sn.get("title", ""), text=text, published_at=sn.get("publishedAt"),
                    author=sn.get("channelTitle"),
                    meta={"views": int(stats.get("viewCount", 0)), "likes": int(stats.get("likeCount", 0) or 0),
                          "comments": int(stats.get("commentCount", 0) or 0), "api": "youtube_data_v3"}))
            cursor["seen"] = (seen + [v["id"] for v in videos if v["id"] not in seen])[-SEEN_KEEP:]
            return SyncResult(items, cursor, warnings=warnings)
        except RosError as exc:
            return SyncResult([], cursor, complete=False, error=exc)

    def _comments(self, video_id: str) -> str:
        data = self._get("commentThreads", 1, part="snippet", videoId=video_id, maxResults=self.comments_per_video,
                         order="relevance", textFormat="plainText")
        lines = []
        for t in data.get("items", []):
            c = t["snippet"]["topLevelComment"]["snippet"]
            lines.append(f"- {c.get('authorDisplayName', '?')} ({c.get('likeCount', 0)} likes): "
                         f"{' '.join(c.get('textDisplay', '').split())[:500]}")
        return "\n\nComentarios destacados:\n" + "\n".join(lines) if lines else ""


# ============================================================================ Reddit
class RedditAPIConnector(_APIConnector):
    """Subreddit listings and searches with full posts and top comments via Reddit's OAuth API."""

    connector_id = "reddit_api"
    api_name = "Reddit API"
    comments_per_post = 10
    posts_with_comments = 5

    def __init__(self, db: Database, id_env: str = "REDDIT_CLIENT_ID", secret_env: str = "REDDIT_CLIENT_SECRET",
                 **kw: Any):
        super().__init__(db, **kw)
        self.id_env, self.secret_env = id_env, secret_env
        self._token: tuple[str, float] | None = None

    def missing_credentials(self) -> list[str]:
        return [e for e in (self.id_env, self.secret_env) if not self.env.get(e)]

    def capabilities(self) -> Capabilities:
        return Capabilities(self.connector_id, self.access_mode, ("validate", "sync", "search"), cursor=True,
                            notes="API OAuth oficial (solo lectura, app-only): posts completos y comentarios "
                                  "destacados. Sujeto a la aprobación y términos actuales de Reddit.")

    def _auth(self) -> str:
        if self._token and self._token[1] > time.time() + 60:
            return self._token[0]
        basic = base64.b64encode(f"{self.env.get(self.id_env, '')}:{self.env.get(self.secret_env, '')}".encode()).decode()
        data = self._request("POST", "https://www.reddit.com/api/v1/access_token",
                             data={"grant_type": "client_credentials"},
                             headers={"Authorization": f"Basic {basic}", "User-Agent": USER_AGENT})
        if "access_token" not in data:
            raise RosError(ErrorKind.INVALID_CREDENTIALS, "Reddit API: no se obtuvo token")
        self._token = (data["access_token"], time.time() + float(data.get("expires_in", 3600)))
        return self._token[0]

    def _get(self, path: str, **params: Any) -> Any:
        return self._request("GET", f"https://oauth.reddit.com{path}", params={**params, "raw_json": 1},
                             headers={"Authorization": f"Bearer {self._auth()}", "User-Agent": USER_AGENT})

    def _probe(self) -> str:
        self._auth()
        return "token OAuth obtenido"

    @staticmethod
    def _path(locator: str) -> tuple[str, dict]:
        loc = locator.strip()
        if loc.lower().startswith("search:"):
            return "/search", {"q": loc[7:].strip(), "sort": "new", "limit": 50}
        m = re.search(r"(?:^|/)r/([A-Za-z0-9_]{2,21})", loc) or re.fullmatch(r"([A-Za-z0-9_]{2,21})", loc)
        if not m:
            raise RosError(ErrorKind.POLICY_REJECTED, f"subreddit no válido: {locator!r} (usa r/nombre o search:texto)")
        return f"/r/{m.group(1)}/new", {"limit": 50}

    def validate(self, locator: str) -> SourceSpec:
        self._require()
        path, params = self._path(locator)
        if path == "/search":
            return SourceSpec(self.connector_id, locator.strip(), f"Reddit: {params['q']}")
        name = path.split("/")[2]
        about = self._get(f"/r/{name}/about")
        return SourceSpec(self.connector_id, f"r/{name}", about.get("data", {}).get("title") or f"r/{name}")

    def sync(self, spec: SourceSpec, cursor: dict | None) -> SyncResult:
        cursor = dict(cursor or {})
        try:
            self._require()
            path, params = self._path(spec.locator)
            listing = self._get(path, **params)
            posts = [c["data"] for c in listing.get("data", {}).get("children", []) if c.get("kind") == "t3"]
            seen = self._seen(cursor)
            items, warnings, commented = [], [], 0
            for p in posts:
                text = p.get("selftext", "") or ""
                if p["name"] not in seen and commented < self.posts_with_comments and p.get("num_comments", 0):
                    commented += 1
                    try:
                        text += self._comments(p["id"])
                    except RosError as exc:
                        warnings.append(f"comentarios de {p['name']}: {exc}")
                url = f"https://www.reddit.com{p.get('permalink', '')}"
                items.append(NormalizedItem(
                    external_id=p["name"], url=url, title=p.get("title", ""),
                    text=text or p.get("url", ""),
                    published_at=datetime.fromtimestamp(p.get("created_utc", 0), timezone.utc).isoformat(),
                    author=p.get("author"),
                    meta={"score": p.get("score", 0), "comments": p.get("num_comments", 0),
                          "subreddit": p.get("subreddit"), "link": p.get("url"), "api": "reddit_oauth"}))
            cursor["seen"] = (seen + [p["name"] for p in posts if p["name"] not in seen])[-SEEN_KEEP:]
            return SyncResult(items, cursor, warnings=warnings)
        except RosError as exc:
            return SyncResult([], cursor, complete=False, error=exc)

    def _comments(self, post_id: str) -> str:
        data = self._get(f"/comments/{post_id}", limit=self.comments_per_post, depth=1, sort="top")
        lines = []
        if isinstance(data, list) and len(data) > 1:
            for c in data[1].get("data", {}).get("children", []):
                if c.get("kind") == "t1":
                    d = c["data"]
                    lines.append(f"- u/{d.get('author', '?')} ({d.get('score', 0)} puntos): "
                                 f"{' '.join(d.get('body', '').split())[:500]}")
        return "\n\nComentarios destacados:\n" + "\n".join(lines) if lines else ""


# ============================================================================ X
class XAPIConnector(_APIConnector):
    """Posts of an account or a recent search via X API v2 (requires a paid entitlement)."""

    connector_id = "x"
    api_name = "X API v2"
    base = "https://api.x.com/2"

    def __init__(self, db: Database, token_env: str = "X_BEARER_TOKEN", daily_requests: int = 200, **kw: Any):
        super().__init__(db, **kw)
        self.token_env = token_env
        self.daily_units = daily_requests

    def missing_credentials(self) -> list[str]:
        return [] if self.env.get(self.token_env) else [self.token_env]

    def capabilities(self) -> Capabilities:
        return Capabilities(self.connector_id, self.access_mode, ("validate", "sync", "search"), cursor=True,
                            notes="Solo lo que permita tu nivel de acceso de pago a la API v2. Peticiones diarias "
                                  "limitadas localmente. Sin acceso: no disponible (nunca scraping).")

    def _get(self, path: str, **params: Any) -> dict:
        self._spend(1)
        return self._request("GET", f"{self.base}{path}", params=params,
                             headers={"Authorization": f"Bearer {self.env.get(self.token_env, '')}"})

    def _probe(self) -> str:
        self._get("/users/by/username/XDevelopers")
        return "ok"

    def validate(self, locator: str) -> SourceSpec:
        self._require()
        loc = locator.strip()
        if loc.lower().startswith("search:"):
            return SourceSpec(self.connector_id, loc, f"X: {loc[7:].strip()}")
        handle = re.sub(r"^.*?(?:x|twitter)\.com/", "", loc).lstrip("@").strip("/")
        data = self._get(f"/users/by/username/{handle}")
        user = data.get("data")
        if not user:
            raise RosError(ErrorKind.CONTENT_DELETED, f"cuenta de X no encontrada: {locator!r}")
        return SourceSpec(self.connector_id, f"id:{user['id']}", f"@{user.get('username', handle)}")

    def sync(self, spec: SourceSpec, cursor: dict | None) -> SyncResult:
        cursor = dict(cursor or {})
        try:
            self._require()
            params: dict[str, Any] = {"max_results": 20, "tweet.fields": "created_at,public_metrics,author_id"}
            if cursor.get("since_id"):
                params["since_id"] = cursor["since_id"]
            if spec.locator.lower().startswith("search:"):
                data = self._get("/tweets/search/recent", query=spec.locator[7:].strip(), **params)
            else:
                data = self._get(f"/users/{spec.locator.removeprefix('id:')}/tweets", **params)
            tweets = data.get("data", [])
            items = [NormalizedItem(external_id=t["id"], url=f"https://x.com/i/web/status/{t['id']}",
                                    title=" ".join(t.get("text", "").split())[:120], text=t.get("text", ""),
                                    published_at=t.get("created_at"), author=t.get("author_id"),
                                    meta={**t.get("public_metrics", {}), "api": "x_v2"}) for t in tweets]
            newest = data.get("meta", {}).get("newest_id")
            if newest:
                cursor["since_id"] = newest
            return SyncResult(items, cursor)
        except RosError as exc:
            return SyncResult([], cursor, complete=False, error=exc)


# ============================================================================ Meta
class InstagramConnector(_APIConnector):
    """Public media of *professional* Instagram accounts through Graph API business discovery."""

    connector_id = "instagram"
    api_name = "Instagram Graph API"

    def __init__(self, db: Database, token_env: str = "META_ACCESS_TOKEN", ig_user_id: str = "", **kw: Any):
        super().__init__(db, **kw)
        self.token_env, self.ig_user_id = token_env, ig_user_id

    def missing_credentials(self) -> list[str]:
        missing = [] if self.env.get(self.token_env) else [self.token_env]
        return missing + ([] if self.ig_user_id else ["instagram_user_id (ros.toml)"])

    def capabilities(self) -> Capabilities:
        return Capabilities(self.connector_id, self.access_mode, ("validate", "sync"), cursor=True,
                            notes="Solo cuentas profesionales (empresa/creador) vía business discovery, con tu cuenta "
                                  "profesional autorizada. No hay búsqueda ni seguimiento de cuentas personales.")

    def _discover(self, username: str, fields: str) -> dict:
        data = self._request("GET", f"{GRAPH}/{self.ig_user_id}",
                             params={"fields": f"business_discovery.username({username}){{{fields}}}",
                                     "access_token": self.env.get(self.token_env, "")})
        if "business_discovery" not in data:
            raise RosError(ErrorKind.CONTENT_DELETED, f"Instagram: @{username} no es una cuenta profesional accesible")
        return data["business_discovery"]

    def _probe(self) -> str:
        data = self._request("GET", f"{GRAPH}/{self.ig_user_id}",
                             params={"fields": "username", "access_token": self.env.get(self.token_env, "")})
        return f"cuenta autorizada: @{data.get('username', '?')}"

    def validate(self, locator: str) -> SourceSpec:
        self._require()
        username = re.sub(r"^.*?instagram\.com/", "", locator.strip()).lstrip("@").strip("/")
        if not re.fullmatch(r"[A-Za-z0-9_.]{1,30}", username):
            raise RosError(ErrorKind.POLICY_REJECTED, f"usuario de Instagram no válido: {locator!r}")
        bd = self._discover(username, "username,name")
        return SourceSpec(self.connector_id, username, bd.get("name") or f"@{username}")

    def sync(self, spec: SourceSpec, cursor: dict | None) -> SyncResult:
        cursor = dict(cursor or {})
        try:
            self._require()
            bd = self._discover(spec.locator, "media.limit(25){id,caption,permalink,timestamp,media_type,"
                                              "like_count,comments_count}")
            media = bd.get("media", {}).get("data", [])
            items = [NormalizedItem(external_id=m["id"], url=m.get("permalink"),
                                    title=" ".join((m.get("caption") or "").split())[:120] or m.get("media_type", ""),
                                    text=m.get("caption") or "", published_at=m.get("timestamp"), author=spec.locator,
                                    meta={"likes": m.get("like_count"), "comments": m.get("comments_count"),
                                          "media_type": m.get("media_type"), "api": "instagram_graph"})
                     for m in media]
            return SyncResult(items, cursor)
        except RosError as exc:
            return SyncResult([], cursor, complete=False, error=exc)


class FacebookConnector(_APIConnector):
    """Posts of a Facebook Page through the Graph API (needs Page access or Page Public Content Access)."""

    connector_id = "facebook"
    api_name = "Facebook Graph API"

    def __init__(self, db: Database, token_env: str = "META_ACCESS_TOKEN", **kw: Any):
        super().__init__(db, **kw)
        self.token_env = token_env

    def missing_credentials(self) -> list[str]:
        return [] if self.env.get(self.token_env) else [self.token_env]

    def capabilities(self) -> Capabilities:
        return Capabilities(self.connector_id, self.access_mode, ("validate", "sync"), cursor=True,
                            notes="Solo Páginas con permisos concedidos (tu Página o Page Public Content Access). "
                                  "No se siguen perfiles personales.")

    def _get(self, path: str, **params: Any) -> dict:
        return self._request("GET", f"{GRAPH}/{path}", params={**params, "access_token": self.env.get(self.token_env, "")})

    def _probe(self) -> str:
        data = self._get("me", fields="id,name")
        return f"token de {data.get('name', '?')}"

    def validate(self, locator: str) -> SourceSpec:
        self._require()
        page = re.sub(r"^.*?facebook\.com/", "", locator.strip()).strip("/")
        data = self._get(page, fields="id,name")
        return SourceSpec(self.connector_id, data["id"], data.get("name", page))

    def sync(self, spec: SourceSpec, cursor: dict | None) -> SyncResult:
        cursor = dict(cursor or {})
        try:
            self._require()
            data = self._get(f"{spec.locator}/posts", fields="id,message,permalink_url,created_time", limit=25)
            items = [NormalizedItem(external_id=p["id"], url=p.get("permalink_url"),
                                    title=" ".join((p.get("message") or "").split())[:120] or p["id"],
                                    text=p.get("message") or "", published_at=p.get("created_time"),
                                    meta={"api": "facebook_graph"}) for p in data.get("data", [])]
            return SyncResult(items, cursor)
        except RosError as exc:
            return SyncResult([], cursor, complete=False, error=exc)
