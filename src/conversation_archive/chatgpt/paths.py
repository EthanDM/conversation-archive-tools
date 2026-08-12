"""Resolve local ChatGPT archive paths with environment overrides taking precedence."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


APP_SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "Conversation Archive Tools"
CONFIG_PATH = APP_SUPPORT_DIR / "config.json"
DEFAULT_DB_PATH = APP_SUPPORT_DIR / "current.sqlite"
DEFAULT_HISTORICAL_DB_PATH = APP_SUPPORT_DIR / "historical.sqlite"
DEFAULT_OUTPUT_DIR = APP_SUPPORT_DIR / "output"


def load_config() -> dict[str, Any]:
    """Load the optional local JSON configuration, returning an empty mapping when absent."""
    path = Path(os.environ.get("CHATGPT_EXPORT_CONFIG", CONFIG_PATH))
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return data


def write_config(config: dict[str, Any]) -> Path:
    path = Path(os.environ.get("CHATGPT_EXPORT_CONFIG", CONFIG_PATH))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return path


def default_db_path() -> str:
    """Return the active-index path from environment, config, or the local default."""
    environment = os.environ.get("CHATGPT_EXPORT_DB")
    if environment:
        return environment
    configured = load_config().get("runtime_db")
    return str(Path(configured).expanduser()) if configured else str(DEFAULT_DB_PATH)


def default_historical_db_path() -> str:
    environment = os.environ.get("CHATGPT_HISTORICAL_DB")
    if environment:
        return environment
    configured = load_config().get("historical_db")
    return (
        str(Path(configured).expanduser())
        if configured
        else str(DEFAULT_HISTORICAL_DB_PATH)
    )


def default_export_path() -> str:
    """Return the configured export path, preferring an explicit environment override."""
    environment = os.environ.get("CHATGPT_EXPORT_INPUT")
    if environment:
        return environment
    config = load_config()
    archive_root = config.get("archive_root")
    current_export = config.get("current_export")
    if archive_root and current_export:
        return str(Path(archive_root).expanduser() / "exports" / "full" / current_export)
    return "."


def default_output_dir() -> Path:
    environment = os.environ.get("CONVERSATION_ARCHIVE_OUTPUT")
    return Path(environment).expanduser() if environment else DEFAULT_OUTPUT_DIR


def configured_archive_root() -> Path:
    value = load_config().get("archive_root")
    if not value:
        raise RuntimeError(
            f"Archive root is not configured. Run conversation-archive-configure first ({CONFIG_PATH})."
        )
    return Path(value).expanduser()
