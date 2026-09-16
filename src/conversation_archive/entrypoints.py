from __future__ import annotations

import sys
from typing import Callable

from .chatgpt.configure import main as configure_main
from .chatgpt.audit import main as audit_chatgpt_main
from .chatgpt.handoff import main as handoff_chatgpt_main
from .chatgpt.historical_index import main as index_historical_main
from .chatgpt.historical_search import main as search_historical_main
from .chatgpt.index import main as index_chatgpt_main
from .chatgpt.refresh import main as refresh_chatgpt_main
from .chatgpt.search import main as search_chatgpt_main
from .chatgpt.show import main as show_chatgpt_main
from .codex.context_candidates import main as context_candidates_main
from .codex.index import main as index_codex_main
from .codex.publish import main as publish_codex_main
from .codex.search import main as search_codex_main
from .codex.show import main as show_codex_main
from .chatgpt.reflection_indexes import main as build_reflection_indexes_main
from .dashboard.server import main as serve_main


def _run(command: Callable[[list[str]], int]) -> int:
    return command(sys.argv[1:])


def configure() -> int:
    return _run(configure_main)


def audit_chatgpt() -> int:
    return _run(audit_chatgpt_main)


def handoff_chatgpt() -> int:
    return _run(handoff_chatgpt_main)


def index_chatgpt() -> int:
    return _run(index_chatgpt_main)


def search_chatgpt() -> int:
    return _run(search_chatgpt_main)


def show_chatgpt() -> int:
    return _run(show_chatgpt_main)


def refresh_chatgpt() -> int:
    return _run(refresh_chatgpt_main)


def index_codex() -> int:
    return _run(index_codex_main)


def publish_codex() -> int:
    return _run(publish_codex_main)


def search_codex() -> int:
    return _run(search_codex_main)


def show_codex() -> int:
    return _run(show_codex_main)


def context_candidates() -> int:
    return _run(context_candidates_main)


def index_historical() -> int:
    return _run(index_historical_main)


def search_historical() -> int:
    return _run(search_historical_main)


def build_reflection_indexes() -> int:
    return _run(build_reflection_indexes_main)


def serve() -> int:
    return _run(serve_main)
