from pathlib import Path
import os


def default_input_path() -> str:
    return os.environ.get("CLAUDE_CODE_SESSION_INPUT", str(Path.home() / ".claude/projects"))


def default_db_path() -> str:
    return os.environ.get("CLAUDE_CODE_SESSION_DB", str(Path.home() / "Library/Application Support/Conversation Archive Tools/claude_code_sessions.sqlite"))
