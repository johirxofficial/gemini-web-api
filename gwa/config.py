"""Configuration loading for gemini-web-api.

Small YAML file + optional environment overrides. No pydantic, no FastAPI —
the project deliberately depends only on websocket-client and PyYAML.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config.yaml"
EXAMPLE_CONFIG = ROOT / "config.example.yaml"
LOGS = ROOT / "logs"
DATA = ROOT / "data"
SCREENSHOTS = LOGS / "screenshots"


class Config:
    def __init__(self, data: dict, path: Path):
        self.data = data or {}
        self.path = path

    def get(self, dotted: str, default: Any = None) -> Any:
        value: Any = self.data
        for part in dotted.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return default if value is None else value

    def set(self, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        node = self.data
        for part in parts[:-1]:
            if not isinstance(node.get(part), dict):
                node[part] = {}
            node = node[part]
        node[parts[-1]] = value

    @property
    def host(self) -> str:
        return str(self.get("api.host", "127.0.0.1"))

    @property
    def port(self) -> int:
        return int(self.get("api.port", 8787))

    @property
    def model(self) -> str:
        return str(self.get("api.model_name", "gemini-web"))

    @property
    def timeout(self) -> int:
        return int(self.get("api.timeout", 180))

    @property
    def api_key(self) -> str:
        return str(self.get("api.api_key", "") or "")


def _env_overrides(cfg: Config) -> None:
    def as_bool(value: str) -> bool:
        return value.strip().lower() in ("1", "true", "yes", "on")

    pairs = {
        "GWA_HOST": ("api.host", str),
        "GWA_PORT": ("api.port", int),
        "GWA_API_KEY": ("api.api_key", str),
        "GWA_MODEL": ("api.model_name", str),
        "GWA_TIMEOUT": ("api.timeout", int),
        "GWA_CDP": ("browser.cdp_url", str),
        "GWA_PREFER_CDP": ("browser.prefer_cdp", str),
        "GWA_PROFILE": ("browser.profile_dir", str),
        "GWA_ENGINE": ("browser.engine", str),
        "GWA_LOCK_DIR": ("api.lock_dir", str),
        "GWA_LOG_PROMPTS": ("api.log_prompts", as_bool),
        "GWA_REUSE_TAB": ("gemini.reuse_tab", as_bool),
    }
    for env, (key, kind) in pairs.items():
        raw = os.environ.get(env)
        if raw is None or raw == "":
            continue
        try:
            cfg.set(key, kind(raw))
        except (TypeError, ValueError):
            pass


def load(path: str | Path | None = None) -> Config:
    chosen = Path(path or os.environ.get("GWA_CONFIG") or DEFAULT_CONFIG).expanduser()
    if not chosen.exists() and chosen == DEFAULT_CONFIG and EXAMPLE_CONFIG.exists():
        shutil.copyfile(EXAMPLE_CONFIG, DEFAULT_CONFIG)
    if not chosen.exists():
        raise FileNotFoundError(f"Config file not found: {chosen}")
    raw = yaml.safe_load(chosen.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"Invalid config.yaml: {chosen}")
    cfg = Config(raw, chosen)
    _env_overrides(cfg)
    return cfg


def ensure_dirs() -> None:
    for folder in (LOGS, DATA, SCREENSHOTS):
        folder.mkdir(parents=True, exist_ok=True)
