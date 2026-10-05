"""One resolved, immutable configuration built at the composition root.

Precedence (highest first): CLI flags > ROS_* environment > --config file >
./ros.toml > ~/.config/ros/config.toml > built-in defaults.
Secrets are never stored in config: only the *name* of the env var that holds them.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any

from .budget import Budget


def ros_home() -> Path:
    return Path(os.environ.get("ROS_HOME", "~/.ros")).expanduser()


@dataclass(frozen=True)
class Settings:
    db_path: str = ""
    reports_dir: str = ""
    # Model per role. Orchestrator: plans, analyses rounds and writes the report. Validator: checks
    # the report against its evidence. Worker: high-volume extraction. Empty = the provider's default
    # for that role (see PROVIDER_DEFAULTS); an empty effort is not sent.
    orchestrator_model: str = ""
    orchestrator_effort: str = ""
    validator_model: str = ""
    validator_effort: str = ""
    worker_model: str = ""
    worker_effort: str = ""
    anthropic_workspace_id: str = ""        # needed when the API key is not scoped to a workspace
    llm: str = "openrouter"                 # openrouter | anthropic | fake (offline demo, no real analysis)
    openrouter_model: str = "spastealth/space-bunny-alpha"   # default model for every role on OpenRouter
    openrouter_api_key_env: str = "OPENROUTER_API_KEY"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    search_backend: str = "duckduckgo"      # duckduckgo | brave | searxng
    searxng_url: str = "http://localhost:8888"
    brave_api_key_env: str = "BRAVE_API_KEY"
    max_fetch_bytes: int = 3_000_000
    doc_chars: int = 12_000                 # max characters of one document sent to the model
    exclude_domains: tuple[str, ...] = ()
    budget: Budget = field(default_factory=Budget)
    timezone: str = "UTC"                   # IANA zone for schedules ("a las 20:00")
    # Monitoring: global hard cap on model spend by all watches per UTC day.
    monitor_daily_cost_usd: float = 3.0
    # Native connectors (official APIs). Only the NAME of the env var holding each secret is stored.
    youtube_api_key_env: str = "YOUTUBE_API_KEY"
    youtube_daily_quota: int = 10_000
    reddit_client_id_env: str = "REDDIT_CLIENT_ID"
    reddit_client_secret_env: str = "REDDIT_CLIENT_SECRET"
    x_bearer_token_env: str = "X_BEARER_TOKEN"
    meta_access_token_env: str = "META_ACCESS_TOKEN"
    instagram_user_id: str = ""             # professional account used for business discovery
    # Delivery of alerts/digests outside the local inbox. Empty = channel off.
    notify_kinds: tuple[str, ...] = ("alert", "digest", "attention")
    notify_webhook_url: str = ""
    notify_webhook_secret_env: str = "ROS_WEBHOOK_SECRET"
    notify_telegram_chat_id: str = ""
    notify_telegram_token_env: str = "ROS_TELEGRAM_TOKEN"
    notify_email_to: str = ""
    notify_email_from: str = ""
    notify_smtp_host: str = ""
    notify_smtp_port: int = 587
    notify_smtp_user: str = ""
    notify_smtp_password_env: str = "ROS_SMTP_PASSWORD"
    # Local API / dashboard (loopback only unless explicitly changed).
    api_host: str = "127.0.0.1"
    api_port: int = 8765
    obsidian_vault: str = ""

    def role(self, name: str) -> tuple[str, str]:
        """(model, effort) for a role: orchestrator | validator | worker."""
        if name not in ROLES:
            raise ValueError(f"unknown model role {name!r}")
        if self.llm == "openrouter":
            default = (self.openrouter_model, "")
        else:
            default = PROVIDER_DEFAULTS["anthropic"][name]
        return (getattr(self, f"{name}_model") or default[0], getattr(self, f"{name}_effort") or default[1])

    def redacted(self) -> dict[str, Any]:
        data = asdict(self)
        data["exclude_domains"] = list(self.exclude_domains)
        data["notify_kinds"] = list(self.notify_kinds)
        return data


ROLES = ("orchestrator", "validator", "worker")

# Per-role defaults when the role's model is not configured. On OpenRouter every role uses
# `openrouter_model` unless a role model is set explicitly.
PROVIDER_DEFAULTS = {
    "anthropic": {"orchestrator": ("claude-opus-5-5", "medium"), "validator": ("claude-sonnet-5-5", "medium"),
                  "worker": ("claude-haiku-4-5", "")},   # Claude Haiku 4.5 does not accept an effort level
}

ENV_MAP = {
    "ROS_DB": "db_path", "ROS_REPORTS_DIR": "reports_dir", "ROS_LLM": "llm", "ROS_SEARCH": "search_backend",
    "ROS_SEARXNG_URL": "searxng_url", "ANTHROPIC_WORKSPACE_ID": "anthropic_workspace_id",
    "ROS_TIMEZONE": "timezone", "ROS_OBSIDIAN_VAULT": "obsidian_vault", "ROS_OPENROUTER_MODEL": "openrouter_model",
    **{f"ROS_{r.upper()}_MODEL": f"{r}_model" for r in ROLES},
    **{f"ROS_{r.upper()}_EFFORT": f"{r}_effort" for r in ROLES},
}


def _apply(settings: Settings, data: dict[str, Any], origin: str) -> Settings:
    known = {f.name for f in fields(Settings)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"{origin}: claves desconocidas {sorted(unknown)}")
    data = dict(data)
    if "budget" in data:
        merged = {**settings.budget.to_dict(), **data["budget"]}
        data["budget"] = Budget.from_dict(merged)
    for key in ("exclude_domains", "notify_kinds"):
        if key in data:
            data[key] = tuple(data[key])
    return replace(settings, **data)


def load_settings(config_path: str | None = None, overrides: dict[str, Any] | None = None) -> Settings:
    settings = Settings()
    candidates = [Path("~/.config/ros/config.toml").expanduser(), Path("ros.toml")]
    for path in candidates:
        if path.is_file():
            settings = _apply(settings, tomllib.loads(path.read_text()), str(path))
    if config_path:
        path = Path(config_path).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"config file not found: {path}")
        settings = _apply(settings, tomllib.loads(path.read_text()), str(path))
    env = {ENV_MAP[k]: v for k, v in os.environ.items() if k in ENV_MAP}
    if env:
        settings = _apply(settings, env, "environment")
    if overrides:
        settings = _apply(settings, {k: v for k, v in overrides.items() if v is not None}, "command line")
    home = ros_home()
    if not settings.db_path:
        settings = replace(settings, db_path=str(home / "ros.db"))
    if not settings.reports_dir:
        settings = replace(settings, reports_dir=str(home / "reports"))
    return settings
