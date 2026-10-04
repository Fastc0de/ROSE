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
    model: str = "claude-opus-5-5"
    effort: str = "medium"
    llm: str = "anthropic"                  # anthropic | fake (offline demo, no real analysis)
    search_backend: str = "duckduckgo"      # duckduckgo | brave | searxng
    searxng_url: str = "http://localhost:8888"
    brave_api_key_env: str = "BRAVE_API_KEY"
    max_fetch_bytes: int = 3_000_000
    doc_chars: int = 12_000                 # max characters of one document sent to the model
    exclude_domains: tuple[str, ...] = ()
    budget: Budget = field(default_factory=Budget)

    def redacted(self) -> dict[str, Any]:
        data = asdict(self)
        data["exclude_domains"] = list(self.exclude_domains)
        return data


ENV_MAP = {
    "ROS_DB": "db_path", "ROS_REPORTS_DIR": "reports_dir", "ROS_MODEL": "model", "ROS_EFFORT": "effort",
    "ROS_LLM": "llm", "ROS_SEARCH": "search_backend", "ROS_SEARXNG_URL": "searxng_url",
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
    if "exclude_domains" in data:
        data["exclude_domains"] = tuple(data["exclude_domains"])
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
